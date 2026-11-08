"""Read-only status views and local checkpoint plans. No AI or delivery.

Keep full private documents at their original paths. A status observation is not
authorization to consume: the existing fresh preflight/lock/ACK checks still run.
"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

from risk_queue import JOB_ID, digest
from risk_result_validator import utc_time


PAYLOAD_FIELDS = frozenset({'input', 'input_parts', 'body_manifest',
                            'raw_articles', 'seen_after_snapshot'})
STATES = frozenset({'pending', 'sending', 'uncertain', 'delivery_complete',
                    'completed', 'verified', 'expired', 'cancelled', 'closed_partial',
                    'delivery_rejected', 'delivery_failed'})


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def instruction_context():
    """Select current subscription instructions without changing stored contracts.

    Run from the pinned checkout. Whole prompt hashes are verified even though
    legacy API text is omitted from the model's context. No queue/network access.
    """
    from risk_subscription import policy
    rules = policy()
    if any(rules.get(k) != 2 for k in ('delivery_focus_version', 'review_pass_version',
                                      'action_quality_version', 'exclusion_review_version')):
        raise ValueError('현재 구독 v2 지침 추출 전용 — 구형 회차는 고정 계약 사용')
    selected, sources = {}, {}
    for key, name, start, end in (
            ('filter', 'filter_prompt.txt', '[구독 모드 적용 범위]', '[기존 API 전용 중복 기사 처리]'),
            ('action', 'action_prompt.txt', '[구독 action_quality_version=2]', '[아래는 기존 API 및 action_quality_version=1 전용]'),
            ('operations', 'RUNBOOK.md', '<!-- subscription-operations:start -->', '<!-- subscription-operations:end -->')):
        raw = Path(name).read_bytes()
        if key in ('filter', 'action') and hashlib.sha256(raw).hexdigest() != rules[key+'_rules']['sha256']:
            raise ValueError('지침 추출 중 원본 변경: '+name)
        text = raw.decode('utf-8')
        if text.count(start) != 1 or text.count(end) != 1 or text.index(start) >= text.index(end):
            raise ValueError('적용 지침 경계 오류: '+name)
        selected[key] = text[text.index(start)+len(start):text.index(end)].strip()
        if not selected[key]:
            raise ValueError('빈 적용 지침: '+name)
        sources[name] = {'sha256': hashlib.sha256(raw).hexdigest(),
                         'source_chars': len(text), 'selected_chars': len(selected[key])}
    schema = Path('risk_result_schema.json').read_bytes()
    sources['risk_result_schema.json'] = {'sha256': hashlib.sha256(schema).hexdigest()}
    return dict(selected, policy=rules, policy_hash=digest(rules),
                schema=json.loads(schema), sources=sources, scope='new_production_v2_only')


def status_view(job, blob_sha):
    """Preserve every non-payload field, including unknown future metadata/errors.

    Source text/old ledgers are not classification inputs at this stage. References
    retain exact content hashes; semantic analysis must still load the full input.
    """
    if not isinstance(job, dict) or not JOB_ID.fullmatch(job.get('job_id', '')):
        raise ValueError('Invalid job identity')
    if job.get('status') not in STATES:
        raise ValueError('Unknown job state; cannot report an empty queue')
    if not isinstance(blob_sha, str) or not blob_sha:
        raise ValueError('Missing observed blob identity')
    result = {k: copy.deepcopy(v) for k, v in job.items() if k not in PAYLOAD_FIELDS}
    refs = {}
    for key in PAYLOAD_FIELDS & job.keys():
        encoded = canonical(job[key]).encode()
        refs[key] = {'sha256': hashlib.sha256(encoded).hexdigest(),
                     'canonical_utf8_bytes': len(encoded)}
    return {'metadata': result, 'source': {
        'path': 'jobs/'+job['job_id']+'.json', 'blob_sha': blob_sha,
        'payload_refs': refs},
        'scope': 'status_only_full_analysis_input_still_required'}


def queue_status(queue, observed_at):
    """Read all indexed jobs, and fail closed on missing/changed index or reads.

    The queue is an injected existing Queue (or offline fixture). No put/source,
    SMTP, model call, retry, expiry cleanup or state mutation occurs here.
    """
    utc_time(observed_at)
    index, initial_sha = queue.get('index.json')
    if not isinstance(index, dict) or not isinstance(index.get('jobs'), list):
        raise ValueError('Missing queue index')
    ids = index['jobs']
    if (any(not isinstance(jid, str) or not JOB_ID.fullmatch(jid) for jid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Invalid or duplicate indexed job ID')
    jobs = []
    for jid in ids:
        job, sha = queue.get('jobs/'+jid+'.json')
        if not isinstance(job, dict) or job.get('job_id') != jid:
            raise ValueError('Missing or mismatched indexed job')
        jobs.append(status_view(job, sha))
    final, final_sha = queue.get('index.json')
    if final_sha != initial_sha or final != index:
        raise ValueError('Queue index changed during observation; fresh read required')
    return {'schema_version': 1, 'observed_at': observed_at,
            'observation_only': True, 'revalidate_before_write': True,
            'index_sha': final_sha, 'jobs': jobs,
            'semantic_analysis_performed': False}


def status_index(view):
    """Small observation index; never label delivery or all analysis complete.

    Full metadata/errors stay in the private status artifact, with exact hashes.
    Inspect that artifact for decisions; this index is only navigation and counts.
    """
    jobs = []
    for entry in view['jobs']:
        m = entry['metadata']
        receipts = m.get('receipts', {})
        counts = {}
        for state in receipts.values():
            counts[state] = counts.get(state, 0)+1
        jobs.append({'job_id': m['job_id'], 'mode': m.get('mode'),
                     'status': m['status'], 'analysis_complete': m.get('analysis_complete'),
                     'input_count': m.get('input_count'),
                     'deferred_count': len(m['deferred_ids']) if 'deferred_ids' in m else None,
                     'receipt_states': counts, 'history_saved': m.get('history_saved'),
                     'ack': m.get('ack'), 'paid_ai_calls': m.get('paid_ai_calls'),
                     'metadata_sha256': digest(m), 'source': entry['source']['path']})
    return {'observed_at': view['observed_at'], 'observation_only': True,
            'full_metadata_required_for_decisions': True, 'jobs': jobs}


def analysis_plan(job, checkpoints=(), max_bytes=12000, body_ids=None):
    """Resume with a first-pass index and selected lossless source pages.

    None retains the full-source view for existing callers; [] exports no body.
    Selecting/omitting a body never classifies it or removes a pending ID.
    """
    if type(max_bytes) is not int or max_bytes < 1000:
        raise ValueError('분석 출력 한도 오류')
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise ValueError('분석 원본 해시 불일치')
    items = {a['article_id']: a for a in job['input']['items']}
    bodies = {b['article_id']: b for b in job['body_manifest']}
    if (len(items) != len(job['input']['items']) or len(bodies) != len(job['body_manifest'])
            or set(items) != set(bodies)):
        raise ValueError('분석 입력 ID 누락·중복')
    proof_required = job.get('review_evidence_version') == 1
    rows, analyses, groups, done = [], {}, [], set()
    narrative_checkpoint = None
    for checkpoint in checkpoints:
        if any(checkpoint.get(k) != job[k] for k in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')):
            raise ValueError('다른 회차·정책 체크포인트')
        if 'narratives' in checkpoint:
            if (job.get('caution_care_version') != 1 or narrative_checkpoint is not None
                    or not isinstance(checkpoint['narratives'], dict)
                    or any(checkpoint.get(k) for k in ('covered_ids', 'classifications', 'analyses', 'event_groups'))):
                raise ValueError('대표기사 문구 체크포인트 계약·중복 오류')
            narrative_checkpoint = checkpoint['narratives']
        ids = checkpoint['covered_ids']
        classified = checkpoint['classifications']
        candidates = {r['article_id'] for r in classified if r['relevant'] is True}
        if (len(ids) != len(set(ids)) or not set(ids) <= items.keys() or done.intersection(ids)
                or len(classified) != len(ids) or {r['article_id'] for r in classified} != set(ids)
                or any(r.get('relevant') is not None and type(r.get('relevant')) is not bool for r in classified)
                or (not set(checkpoint['analyses']) <= candidates if proof_required
                    else set(checkpoint['analyses']) != candidates)):
            raise ValueError('체크포인트 완료 ID·분석 누락·중복')
        if proof_required:
            from risk_review_evidence import validate_decision
            for row in classified:
                validate_decision(row, items[row['article_id']], bodies[row['article_id']],
                                  checkpoint['analyses'].get(row['article_id']))
        done.update(ids)
        rows.extend(copy.deepcopy(classified))
        analyses.update(copy.deepcopy(checkpoint['analyses']))
        groups.extend(copy.deepcopy(checkpoint.get('event_groups', [])))
    missing_body_ids = []
    if job.get('review_pass_version') == 2:
        for aid, body in bodies.items():
            from risk_body_quality import body_quality
            unusable = body['body_status'] in {'missing', 'unreadable'} or (
                proof_required and body_quality(body['body'])[0] != 'verified')
            if aid not in done and unusable:
                row = {'article_id': aid, 'relevant': None, 'reason': '유효 본문 미확보; 새 근거 확보 전 재분석하지 않음'}
                if proof_required:
                    from risk_review_evidence import unavailable_basis
                    row['decision_basis'] = unavailable_basis(body)
                rows.append(row)
                missing_body_ids.append(aid)
                done.add(aid)
    pending = [aid for aid in items if aid not in done]
    if narrative_checkpoint is not None:
        if pending:
            raise ValueError('대표기사 문구는 입력 전수 1차 처리 후 작성')
        for aid, writing in narrative_checkpoint.items():
            if (aid not in analyses or not isinstance(analyses[aid], dict)
                    or bodies[aid]['body_status'] != 'verified'
                    or not isinstance(writing, dict)
                    or not {'action', 'customer_notice'} <= writing.keys()
                    or writing.keys() - {'action', 'customer_notice', 'omission_reason'}):
                raise ValueError('대표기사 문구 ID·필드 오류')
            reason = writing.get('omission_reason', '')
            if (not isinstance(reason, str) or len(reason) > 240
                    or isinstance(writing['action'], str)
                    and not writing['action'].strip() and len(reason.strip()) < 8):
                raise ValueError('대응방안 미작성은 실제 생략 사유 필요')
            # Optional text errors remain per-field omissions in the existing
            # validator; they must not block otherwise valid articles.
            for field in ('action', 'customer_notice'):
                if analyses[aid].get(field) not in ('', None, writing[field]):
                    raise ValueError('이미 작성한 대표기사 문구 덮어쓰기 금지')
                analyses[aid][field] = writing[field]
    selected = set(pending) if body_ids is None else set(body_ids)
    if not selected <= items.keys():
        raise ValueError('본문 요청의 입력 ID 불일치')
    pages, triage_pages, triage = [], [], []
    for aid in pending:
        article, body = items[aid], bodies[aid]
        text = body['body']
        base = {'article_id': aid, 'title': article['title'], 'description': article['description'],
                'url': article['url'], 'published_at': article.get('published_at') or article.get('pubDate'),
                'body_status': body['body_status'], 'body_sha256': hashlib.sha256(text.encode()).hexdigest()}
        # Compact first pass: no body text, implied verdict, or hidden completion.
        if len(canonical([base]).encode()) > max_bytes:
            raise ValueError('기사 메타데이터가 출력 한도 초과; 한도를 늘려 해당 기사만 다시 읽기')
        if len(canonical(triage+[base]).encode()) > max_bytes:
            triage_pages.append(triage)
            triage = []
        triage.append(base)
        if aid not in selected:
            continue
        offset = 0
        while True:
            def page(end):
                return dict(base, offset=offset, end=end, complete=end == len(text), body=text[offset:end])
            if len(canonical(page(offset)).encode()) > max_bytes:
                raise ValueError('기사 메타데이터가 출력 한도 초과; 한도를 늘려 해당 기사만 다시 읽기')
            low, high = offset, len(text)
            while low < high:
                middle = (low+high+1)//2
                if len(canonical(page(middle)).encode()) <= max_bytes:
                    low = middle
                else:
                    high = middle-1
            if low == offset and offset < len(text):
                raise ValueError('본문 출력 한도 부족')
            pages.append(page(low))
            if low == len(text):
                break
            offset = low
    if triage:
        triage_pages.append(triage)
    return {'job_id': job['job_id'], 'input_hash': job['input_hash'], 'policy_hash': job['policy_hash'],
            'classifications': rows, 'analyses': analyses, 'event_groups': groups, 'pending_ids': pending,
            'missing_body_ids': missing_body_ids, 'triage_pages': triage_pages, 'pages': pages,
            'narratives_ready': narrative_checkpoint is not None,
            'scope': 'local_resume_only_final_validation_required'}


def _assemble(job, plan, model_used, attempt_id):
    result = {key: job[key] for key in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')}
    result.update(schema_version=1, policy_version='risk-subscription-v1',
                  analysis_engine='chatgpt_subscription', model_used=model_used,
                  classifications=plan['classifications'], analyses=plan['analyses'],
                  event_groups=plan['event_groups'])
    if job.get('review_evidence_version') == 1:
        if not isinstance(attempt_id, str) or not JOB_ID.fullmatch(attempt_id):
            raise ValueError('최종 결과의 분석 소유자 ID 필요')
        result['review_attempt_id'] = attempt_id
    if plan['pending_ids']:
        result['unprocessed_ids'] = plan['pending_ids']
    return result


def validate_progress(job, checkpoints, attempt_id):
    """Catch immutable/global errors before checkpoint publication, not at SMTP."""
    from risk_result_validator import validate_checkpoint
    plan = analysis_plan(job, checkpoints, body_ids=[])
    if plan['event_groups'] and plan['pending_ids']:
        raise ValueError('동일 사건 묶음은 입력 전수 1차 처리 뒤 한 번 저장')
    result = _assemble(job, plan, 'unconfirmed', attempt_id)
    return validate_checkpoint(result, job, job['input'], job['body_manifest'])


def ready_result(job, checkpoints, model_used='unconfirmed', attempt_id=None, *, narrative_draft=False):
    """New rounds require a first pass of every input; holds need not be resolved."""
    from risk_result_validator import validate
    plan = analysis_plan(job, checkpoints, body_ids=[])
    if job.get('delivery_focus_version') == 2 and plan['pending_ids']:
        raise ValueError('미처리 분석을 계속해야 함 — '+str(len(plan['pending_ids']))+'건')
    if job.get('caution_care_version') == 1 and not narrative_draft and not plan['narratives_ready']:
        raise ValueError('대표기사 대응방안·고객문구 체크포인트 필요 — 문구 초안은 --narrative-draft 사용')
    result = _assemble(job, plan, model_used, attempt_id)
    validate(result, job, job['input'], job['body_manifest'])
    return result


def next_batch(job, checkpoints=(), limit=25):
    """Read the next bounded batch; priority changes order, never verdicts/IDs."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('다음 본문 묶음은 1~100건')
    plan = analysis_plan(job, checkpoints, body_ids=[])
    pending = set(plan['pending_ids'])
    bodies = {b['article_id']: b['body'] for b in job['body_manifest']}
    def priority(article):
        # Source text may reveal a severe incident behind a generic title.
        # This only orders full-source reading; it never supplies a verdict.
        text = article['title']+' '+article['description']+' '+bodies[article['article_id']]
        risk = bool(re.search(r'장애|제재|과태료|상장폐지|횡령|배임|부도|파산|회생|급락|폭락|담보|반대매매', text))
        return (0 if risk and '한국투자증권' in text else 1 if risk else 2)
    candidates = sorted((a for a in job['input']['items'] if a['article_id'] in pending), key=priority)
    selected = [a['article_id'] for a in candidates[:limit]]
    result = analysis_plan(job, checkpoints, body_ids=selected)
    order = {aid:i for i,aid in enumerate(selected)}
    result['pages'].sort(key=lambda p:(order[p['article_id']],p['offset']))
    result['priority_ids'] = selected
    return result


def narrative_targets(job, result, bot):
    """Run native selection locally, stop before writing text, history or sending.

    Caller supplies the frozen exposure/seen files in a private working directory.
    Every external request and SMTP connection is blocked, including fallbacks.
    This is a target plan, not a semantic review or delivery authorization.
    """
    import contextlib
    import io
    import socket
    from pathlib import Path
    from unittest.mock import patch
    from risk_result_validator import validate
    from risk_continuity import delivery_view
    from risk_provider import Provider
    checked = validate(result, job, job['input'], job['body_manifest'])
    for field, filename in (('history_hash', 'seen_news.json'), ('exposure_hash', 'exposure_data.csv')):
        if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != job['input'][field]:
            raise ValueError('대표기사 계획의 고정 이력·잔고 해시 불일치')
    local = copy.deepcopy(job)
    local['_validation_holds'] = checked.get('held_articles', {})
    local['_validation_omissions'] = checked.get('omitted_fields', {})
    local['_unprocessed_ids'] = checked.get('unprocessed_ids', [])
    view, answers = delivery_view(local, result, set())
    provider = Provider(view, answers)
    selected = []
    class Complete(Exception):
        pass
    def stop(rows):
        selected.extend(provider.by_url[a['url']] for a in original_select(rows)
                        if a.get('grade') in ('긴급', '주의'))
        raise Complete()
    def empty(*args, **kwargs):
        raise Complete()
    original_select = bot.dedup_same_mail_events
    names = ('RISK_PROVIDER', 'CLAUDE_MODEL', 'CLAUDE_FILTER_MODEL', 'CLAUDE_ACTION_MODEL',
             'CLAUDE_VERIFY_MODEL', 'CLAUDE_VERIFY_HIGH_MODEL', '_LAST_ANALYSIS_ENGINE',
             '_LAST_VERIFY_MODEL', 'KEYWORDS', 'get_overseas_keywords', 'crawl_naver_news', 'fetch_article_body')
    before = {name: getattr(bot, name, None) for name in names}
    try:
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(patch.object(bot, name, before[name], create=True))
            provider.install(bot)
            stack.enter_context(patch.object(bot, 'dedup_same_mail_events', stop))
            for name in ('send_email', 'send_email_no_result'):
                stack.enter_context(patch.object(bot, name, empty))
            for name in ('save_seen_urls', 'save_run_stats', 'save_filter_log', 'flush_owner_warnings'):
                stack.enter_context(patch.object(bot, name, side_effect=RuntimeError('계획 중 저장·알림 금지')))
            stack.enter_context(patch.object(bot, 'build_price_alert_section', lambda *a, **k: ''))
            stack.enter_context(patch.object(bot, 'load_competitor_notices', return_value=[]))
            stack.enter_context(patch.object(bot.smtplib, 'SMTP_SSL', side_effect=RuntimeError('계획 중 SMTP 금지')))
            stack.enter_context(patch.object(bot.requests.sessions.Session, 'request', side_effect=RuntimeError('계획 중 HTTP 금지')))
            stack.enter_context(patch.object(socket.socket, 'connect', side_effect=RuntimeError('계획 중 외부 접속 금지')))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            bot.main()
    except Complete:
        pass
    return selected


def usage_observation(*, stage, engine, measurement_source, usage=None,
                      elapsed_ms=None, attempt_number=None, retry_reason=None):
    """Preserve absent counters as unknown. Never infer platform usage from API0.

    Anthropic input_tokens excludes cache reads/writes. Cache counters are only
    populated when explicitly provided; unavailable is not a measured zero.
    """
    if measurement_source not in {'provider_adaptation', 'anthropic_response',
                                   'subscription_platform', 'offline_replay'}:
        raise ValueError('Unknown measurement source')
    fields = ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
              'cache_creation_input_tokens')
    if usage is not None and not isinstance(usage, dict):
        raise ValueError('Usage must be explicit counters or unknown')
    counters = {name: (usage or {}).get(name) for name in fields}
    for value in counters.values():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('Invalid measured counter')
    if measurement_source == 'provider_adaptation' and usage is not None:
        raise ValueError('Provider adaptation does not expose model token usage')
    for value in (elapsed_ms, attempt_number):
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
            raise ValueError('Invalid timing/attempt counter')
    return {'stage': stage, 'engine': engine,
            'measurement_source': measurement_source, 'tokens': counters,
            'elapsed_ms': elapsed_ms, 'attempt_number': attempt_number,
            'retry_reason': retry_reason,
            'model_call_count': None,
            'paid_ai_calls': 0 if measurement_source == 'provider_adaptation' else None,
            'cost': None, 'cost_currency': None}


if __name__ == '__main__':
    # Only an already downloaded local snapshot; no implicit live credentials.
    import argparse
    from risk_analysis_guard import strict_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', nargs='?', help='private JSON: index, jobs, observed_at')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--analysis', action='store_true', help='first-pass index; body pages only for --body-id candidates')
    mode.add_argument('--instructions', action='store_true', help='select current instructions from this pinned checkout; no snapshot')
    mode.add_argument('--narrative-draft', action='store_true', help='assemble a local draft for native representative planning; never publish as ready')
    parser.add_argument('--attempt-id', help='analysis owner for --narrative-draft')
    parser.add_argument('--body-id', action='append', default=[], help='with --analysis: pending candidate body to read; repeat for a batch')
    parser.add_argument('--next-batch', type=int, help='with --analysis: next 1~100 pending bodies, priority first; no classifications implied')
    parser.add_argument('--output', required=True, help='new private local status artifact (not an operational result)')
    args = parser.parse_args()
    if args.instructions and args.snapshot or not args.instructions and not args.snapshot:
        parser.error('--instructions takes no snapshot; other modes require one')
    if args.body_id and not args.analysis:
        parser.error('--body-id requires --analysis')
    if args.narrative_draft and not args.attempt_id:
        parser.error('--narrative-draft requires --attempt-id')
    if args.next_batch is not None and (not args.analysis or args.body_id):
        parser.error('--next-batch requires --analysis and cannot combine with --body-id')
    snapshot = {} if args.instructions else strict_json(Path(args.snapshot).read_text())
    class LocalSnapshot:
        def get(self, path):
            if path == 'index.json':
                return snapshot['index'], snapshot.get('index_sha') or digest(snapshot['index'])
            record = snapshot['jobs'].get(path.removeprefix('jobs/').removesuffix('.json'))
            return (record['value'], record['blob_sha']) if record else (None, None)
    view = (instruction_context() if args.instructions else
            {'result': ready_result(snapshot['job'], snapshot.get('checkpoints', []),
                                    attempt_id=args.attempt_id, narrative_draft=True),
             'scope': 'local_narrative_draft_only_not_ready'} if args.narrative_draft else
            next_batch(snapshot['job'], snapshot.get('checkpoints', []), args.next_batch) if args.next_batch is not None else
            analysis_plan(snapshot['job'], snapshot.get('checkpoints', []), body_ids=args.body_id) if args.analysis
            else queue_status(LocalSnapshot(), snapshot['observed_at']))
    content = canonical(view)
    import os
    with Path(args.output).open('x', encoding='utf-8') as file:
        os.chmod(file.name, 0o600)
        file.write(content)
    index = ({'scope': view['scope'], 'policy_hash': view['policy_hash'], 'sources': view['sources']} if args.instructions else
             {'job_id': view['result']['job_id'], 'scope': view['scope']} if args.narrative_draft else
             {'job_id': view['job_id'], 'completed_ids': len(view['classifications']),
              'pending_ids': len(view['pending_ids']), 'missing_body_ids': len(view['missing_body_ids']),
              'triage_pages': len(view['triage_pages']),
              'pages': len(view['pages']), 'scope': view['scope']} if args.analysis else status_index(view))
    index['artifact'] = str(Path(args.output).resolve())
    index['artifact_sha256'] = hashlib.sha256(content.encode()).hexdigest()
    print(canonical(index))
