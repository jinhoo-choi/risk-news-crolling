"""Read-only status views and local checkpoint plans. No AI or delivery.

Keep full private documents at their original paths. A status observation is not
authorization to consume: the existing fresh preflight/lock/ACK checks still run.
"""
import copy
import hashlib
import json
import math
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


def analysis_plan(job, checkpoints=(), max_bytes=12000):
    """Local resume plan with lossless bounded pages; never writes a result or sends."""
    if type(max_bytes) is not int or max_bytes < 1000:
        raise ValueError('분석 출력 한도 오류')
    if digest(job['input']) != job['input_hash'] or digest(job['body_manifest']) != job['body_manifest_hash']:
        raise ValueError('분석 원본 해시 불일치')
    items = {a['article_id']: a for a in job['input']['items']}
    bodies = {b['article_id']: b for b in job['body_manifest']}
    if (len(items) != len(job['input']['items']) or len(bodies) != len(job['body_manifest'])
            or set(items) != set(bodies)):
        raise ValueError('분석 입력 ID 누락·중복')
    rows, analyses, groups, done = [], {}, [], set()
    for checkpoint in checkpoints:
        if any(checkpoint.get(k) != job[k] for k in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')):
            raise ValueError('다른 회차·정책 체크포인트')
        ids = checkpoint['covered_ids']
        classified = checkpoint['classifications']
        if (len(ids) != len(set(ids)) or not set(ids) <= items.keys() or done.intersection(ids)
                or len(classified) != len(ids) or {r['article_id'] for r in classified} != set(ids)
                or any(r.get('relevant') is not None and type(r.get('relevant')) is not bool for r in classified)
                or set(checkpoint['analyses']) != {r['article_id'] for r in classified if r['relevant'] is True}):
            raise ValueError('체크포인트 완료 ID·분석 누락·중복')
        done.update(ids)
        rows.extend(copy.deepcopy(classified))
        analyses.update(copy.deepcopy(checkpoint['analyses']))
        groups.extend(copy.deepcopy(checkpoint.get('event_groups', [])))
    missing_body_ids = []
    if job.get('review_pass_version') == 2:
        for aid, body in bodies.items():
            if aid not in done and body['body_status'] in {'missing', 'unreadable'}:
                rows.append({'article_id': aid, 'relevant': None, 'reason': '유효 본문 미확보; 새 근거 확보 전 재분석하지 않음'})
                missing_body_ids.append(aid)
                done.add(aid)
    pending = [aid for aid in items if aid not in done]
    pages = []
    for aid in pending:
        article, body = items[aid], bodies[aid]
        text = body['body']
        base = {'article_id': aid, 'title': article['title'], 'description': article['description'],
                'url': article['url'], 'published_at': article.get('published_at') or article.get('pubDate'),
                'body_status': body['body_status'], 'body_sha256': hashlib.sha256(text.encode()).hexdigest()}
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
    return {'job_id': job['job_id'], 'input_hash': job['input_hash'], 'policy_hash': job['policy_hash'],
            'classifications': rows, 'analyses': analyses, 'event_groups': groups, 'pending_ids': pending,
            'missing_body_ids': missing_body_ids, 'pages': pages,
            'scope': 'local_resume_only_final_validation_required'}


def ready_result(job, checkpoints, model_used='unconfirmed'):
    """New rounds require a first pass of every input; holds need not be resolved."""
    from risk_result_validator import validate
    plan = analysis_plan(job, checkpoints)
    if job.get('delivery_focus_version') == 2 and plan['pending_ids']:
        raise ValueError('미처리 분석을 계속해야 함 — '+str(len(plan['pending_ids']))+'건')
    result = {key: job[key] for key in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')}
    result.update(schema_version=1, policy_version='risk-subscription-v1',
                  analysis_engine='chatgpt_subscription', model_used=model_used,
                  classifications=plan['classifications'], analyses=plan['analyses'],
                  event_groups=plan['event_groups'])
    if plan['pending_ids']:
        result['unprocessed_ids'] = plan['pending_ids']
    validate(result, job, job['input'], job['body_manifest'])
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
    mode.add_argument('--analysis', action='store_true', help='snapshot contains job and checkpoints; prepare bounded source pages')
    mode.add_argument('--instructions', action='store_true', help='select current instructions from this pinned checkout; no snapshot')
    parser.add_argument('--output', required=True, help='new private local status artifact (not an operational result)')
    args = parser.parse_args()
    if args.instructions and args.snapshot or not args.instructions and not args.snapshot:
        parser.error('--instructions takes no snapshot; other modes require one')
    snapshot = {} if args.instructions else strict_json(Path(args.snapshot).read_text())
    class LocalSnapshot:
        def get(self, path):
            if path == 'index.json':
                return snapshot['index'], snapshot.get('index_sha') or digest(snapshot['index'])
            record = snapshot['jobs'].get(path.removeprefix('jobs/').removesuffix('.json'))
            return (record['value'], record['blob_sha']) if record else (None, None)
    view = (instruction_context() if args.instructions else
            analysis_plan(snapshot['job'], snapshot.get('checkpoints', [])) if args.analysis
            else queue_status(LocalSnapshot(), snapshot['observed_at']))
    content = canonical(view)
    import os
    with Path(args.output).open('x', encoding='utf-8') as file:
        os.chmod(file.name, 0o600)
        file.write(content)
    index = ({'scope': view['scope'], 'policy_hash': view['policy_hash'], 'sources': view['sources']} if args.instructions else
             {'job_id': view['job_id'], 'completed_ids': len(view['classifications']),
              'pending_ids': len(view['pending_ids']), 'missing_body_ids': len(view['missing_body_ids']),
              'pages': len(view['pages']), 'scope': view['scope']} if args.analysis else status_index(view))
    index['artifact'] = str(Path(args.output).resolve())
    index['artifact_sha256'] = hashlib.sha256(content.encode()).hexdigest()
    print(canonical(index))
