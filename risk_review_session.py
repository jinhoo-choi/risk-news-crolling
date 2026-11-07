"""Atomic analysis ownership, immutable checkpoints and final-result publication.

The CLI only prepares a private transaction from an already downloaded snapshot.
It never connects to GitHub, SMTP or a model. The existing Queue adapter can
execute the same functions with leases; connector callers publish the returned
transaction against its exact observed branch head, then read it back.
"""
import copy
import json
import re
from pathlib import Path
from risk_queue import JOB_ID, digest, now
from risk_result_validator import utc_time
from risk_shards import hydrate

KEYS = ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash')


def fresh(queue, path):
    return getattr(queue, 'get_fresh', queue.get)(path)


def pending(queue, jid):
    if not isinstance(jid, str) or not JOB_ID.fullmatch(jid):
        raise ValueError('분석 회차 ID 오류')
    job, sha = fresh(queue, 'jobs/'+jid+'.json')
    if (not job or job.get('mode') != 'production' or job.get('review_evidence_version') != 1
            or job.get('status') != 'pending' or job.get('receipts') or job.get('attempted_count', 0)
            or utc_time(job['expires_at']) <= now()):
        raise ValueError('새 계약의 유효 pending 회차만 분석 가능')
    if fresh(queue, 'outbox/'+jid+'.json')[0] is not None:
        raise ValueError('고정 발송본 존재 — 분석 재시작 금지')
    return hydrate(queue, job), sha


def owner(queue, job, attempt_id, states=('running',)):
    path = 'analysis-attempts/'+job['job_id']+'.json'
    attempt, sha = fresh(queue, path)
    if (not attempt or attempt.get('attempt_id') != attempt_id
            or attempt.get('state') not in states
            or any(attempt.get(k) != job[k] for k in KEYS)):
        raise ValueError('분석 소유권·입력 계약 불일치')
    return path, attempt, sha


def begin(queue, jid, attempt_id, source_main, automation_id):
    if (not isinstance(attempt_id, str) or not JOB_ID.fullmatch(attempt_id) or not automation_id
            or not isinstance(source_main, str) or not re.fullmatch(r'[0-9a-f]{40}',source_main)):
        raise ValueError('분석 소유자·실행 SHA 누락')
    job, job_sha = pending(queue, jid)
    path = 'analysis-attempts/'+jid+'.json'
    old, old_sha = fresh(queue, path)
    if old and old.get('attempt_id') == attempt_id:
        owner(queue, job, attempt_id, ('running', 'ready'))
        return old  # Exact same owner resumes; no duplicate checkpoint writes.
    if old and old.get('state') != 'blocked':
        raise ValueError('다른 분석 소유자 실행 중 — heartbeat로 탈취 금지')
    if fresh(queue, 'results/'+jid+'.json')[0] is not None:
        raise ValueError('저장 결과부터 소비·복구 — 재분석 금지')
    stamp = now().isoformat()
    attempt = dict((k, job[k]) for k in KEYS)
    attempt.update(attempt_id=attempt_id, source_main=source_main, automation_id=automation_id,
                   state='running', stage='analyzing', started_at=stamp, updated_at=stamp,
                   input_count=len(job['input']['items']), processed_count=0,
                   covered_ids=[], checkpoint_refs=[], errors=[])
    attempt['first_started_at'] = old.get('first_started_at',old.get('started_at',stamp)) if old else stamp
    values = {path: attempt}
    expected = {path: old_sha, 'jobs/'+jid+'.json': job_sha,
                'results/'+jid+'.json': None, 'outbox/'+jid+'.json': None}
    if old:
        if not isinstance(old.get('attempt_id'), str) or not JOB_ID.fullmatch(old['attempt_id']):
            raise ValueError('기존 소유자 ID 오류 — 이력 인계 금지')
        archive = 'audit/'+jid+'/'+old['attempt_id']+'-ended.json'
        previous, previous_sha = fresh(queue, archive)
        if previous is not None and previous != old:
            raise ValueError('종료된 분석 이력 덮어쓰기 금지')
        values[archive], expected[archive] = old, previous_sha
        # Reuse confirmed immutable work, not the previous owner's incomplete text.
        attempt['checkpoint_refs'] = copy.deepcopy(old.get('checkpoint_refs', []))
        checkpoints, checkpoint_leases = read_checkpoints(queue, job, attempt)
        expected.update(checkpoint_leases)
        covered = [aid for cp in checkpoints for aid in cp['covered_ids']]
        attempt.update(covered_ids=covered, processed_count=len(covered))
    queue.put_many(values, expected)
    return attempt


def read_checkpoints(queue, job, attempt):
    prefix = 'analysis-checkpoints/'+job['job_id']+'/'
    checkpoints, leases, paths = [], {}, set()
    if len(attempt.get('checkpoint_refs', [])) > len(job['input']['items'])+1:
        raise ValueError('체크포인트 개수가 입력 범위를 초과')
    for ref in attempt.get('checkpoint_refs', []):
        path = ref['path']
        if (not path.startswith(prefix) or '/' in path[len(prefix):]
                or not path.endswith('.json') or path in paths):
            raise ValueError('체크포인트 경로·중복 오류')
        paths.add(path)
    snapshots = (queue.get_many_fresh(paths) if hasattr(queue,'get_many_fresh')
                 else {path:fresh(queue,path) for path in paths})
    for ref in attempt.get('checkpoint_refs', []):
        path = ref['path']; cp, sha = snapshots[path]
        if not cp or digest(cp) != ref['sha256']:
            raise ValueError('완료 체크포인트 변경·누락')
        checkpoints.append(cp); leases[path] = sha
    from risk_review_context import analysis_plan
    analysis_plan(job, checkpoints, body_ids=[])
    return checkpoints, leases


def save_checkpoint(queue, jid, attempt_id, checkpoint):
    job, job_sha = pending(queue, jid)
    path, attempt, attempt_sha = owner(queue, job, attempt_id)
    from risk_review_evidence import bind_sources
    checkpoint = bind_sources(checkpoint, job)
    from risk_review_context import analysis_plan
    analysis_plan(job, [checkpoint], body_ids=[])
    if not checkpoint['covered_ids'] and not checkpoint.get('event_groups'):
        raise ValueError('빈 체크포인트 저장 금지')
    h = digest(checkpoint)
    if any(ref['sha256'] == h for ref in attempt['checkpoint_refs']):
        read_checkpoints(queue, job, attempt)
        return attempt  # Ambiguous previous write: verify before returning success.
    if set(attempt['covered_ids']).intersection(checkpoint['covered_ids']):
        raise ValueError('완료 ID 재분석·덮어쓰기 금지')
    checkpoints, leases = read_checkpoints(queue, job, attempt)
    from risk_review_context import validate_progress
    validate_progress(job, [*checkpoints, checkpoint], attempt_id)
    # A single final grouping checkpoint may refer to earlier immutable article
    # decisions. No early grouping can become impossible to extend or correct.
    if checkpoint.get('event_groups') and any(cp.get('event_groups') for cp in checkpoints):
        raise ValueError('동일 사건 전체 비교는 한 체크포인트로 저장')
    cp_path = 'analysis-checkpoints/'+jid+'/'+attempt_id+'-'+str(len(attempt['checkpoint_refs'])+1).zfill(4)+'.json'
    attempt['checkpoint_refs'].append({'path': cp_path, 'sha256': h})
    attempt['covered_ids'].extend(checkpoint['covered_ids'])
    attempt.update(processed_count=len(attempt['covered_ids']), updated_at=now().isoformat())
    queue.put_many({path: attempt, cp_path: checkpoint},
                   {**leases, 'jobs/'+jid+'.json': job_sha, path: attempt_sha, cp_path: None,
                    'results/'+jid+'.json': None, 'outbox/'+jid+'.json': None})
    return attempt


def finalize(queue, jid, attempt_id, model_used='unconfirmed'):
    job, job_sha = pending(queue, jid)
    path, attempt, attempt_sha = owner(queue, job, attempt_id, ('running', 'ready'))
    result_path = 'results/'+jid+'.json'
    previous, result_sha = fresh(queue, result_path)
    if attempt['state'] == 'ready':
        verify_ready(queue, job, previous)
        return previous
    if previous is not None:
        raise ValueError('기존 결과를 분석 완료로 덮어쓰기 금지')
    checkpoints, leases = read_checkpoints(queue, job, attempt)
    from risk_review_context import ready_result
    result = ready_result(job, checkpoints, model_used, attempt_id=attempt_id)
    covered = {aid for cp in checkpoints for aid in cp['covered_ids']}
    # Missing/unreadable bodies are source-status decisions, produced by code.
    automatic = [r for r in result['classifications'] if r['article_id'] not in covered]
    values = {}
    if automatic:
        cp = {k: job[k] for k in KEYS}
        cp.update(covered_ids=[r['article_id'] for r in automatic], classifications=automatic,
                  analyses={}, event_groups=[])
        cp_path = 'analysis-checkpoints/'+jid+'/'+attempt_id+'-body-status.json'
        values[cp_path], leases[cp_path] = cp, None
        attempt['checkpoint_refs'].append({'path': cp_path, 'sha256': digest(cp)})
    attempt.update(state='ready', stage='result_ready', result_hash=digest(result),
                   covered_ids=[r['article_id'] for r in result['classifications']],
                   processed_count=len(result['classifications']), updated_at=now().isoformat())
    attempt['result_ready_at'] = attempt['updated_at']
    values.update({path: attempt, result_path: result})
    leases.update({path: attempt_sha, 'jobs/'+jid+'.json': job_sha,
                   result_path: result_sha, 'outbox/'+jid+'.json': None})
    queue.put_many(values, leases)
    return result


def verify_ready(queue, job, result):
    """Return mandatory leases for the atomic pending -> sending transition."""
    if not isinstance(result, dict):
        raise ValueError('분석 결과 누락')
    path, attempt, attempt_sha = owner(queue, job, result.get('review_attempt_id'), ('ready',))
    checkpoints, leases = read_checkpoints(queue, job, attempt)
    from risk_review_context import ready_result
    assembled = ready_result(job, checkpoints, result['model_used'], attempt_id=attempt['attempt_id'])
    ids = {a['article_id'] for a in job['input']['items']}
    if (attempt.get('result_hash') != digest(result) or assembled != result
            or set(attempt['covered_ids']) != ids or len(attempt['covered_ids']) != len(ids)
            or attempt['processed_count'] != len(ids)):
        raise ValueError('분석 완료 집합·저장 결과·소유권 불일치')
    stored, sha = fresh(queue, 'results/'+job['job_id']+'.json')
    if stored != result:
        raise ValueError('소비 검증 중 결과 변경')
    return {**leases, path: attempt_sha, 'results/'+job['job_id']+'.json': sha}


def stop(queue, jid, attempt_id, reason):
    """The active owner explicitly records its end; never inferred from age."""
    job, job_sha = pending(queue, jid)
    path, attempt, sha = owner(queue, job, attempt_id)
    if not isinstance(reason, str) or not 1 <= len(reason) <= 240:
        raise ValueError('종료 사유 필요')
    attempt.update(state='blocked', stage='analyzing', updated_at=now().isoformat())
    attempt['errors'].append({'at': attempt['updated_at'], 'code':'owner_stopped', 'message':reason})
    queue.put_many({path: attempt}, {path: sha, 'jobs/'+jid+'.json':job_sha,
                   'results/'+jid+'.json':None, 'outbox/'+jid+'.json':None})
    return attempt


class SnapshotQueue:
    """An offline transaction planner; absent reads must be explicitly observed."""
    def __init__(self, snapshot):
        self.files = copy.deepcopy(snapshot['files']); self.transactions = []
    def get(self, path):
        if path not in self.files:
            raise ValueError('snapshot에 조회되지 않은 경로: '+path)
        entry = self.files[path]
        return copy.deepcopy(entry['value']), entry['sha']
    get_fresh = get
    def put_many(self, values, expected, deletions=()):
        if deletions:
            raise ValueError('분석 transaction 삭제 금지')
        for path, sha in expected.items():
            # New immutable checkpoint/archive paths are explicitly leased absent.
            if self.files.get(path, {'sha':None})['sha'] != sha:
                raise ValueError('로컬 lease 변경')
        self.transactions.append({'values':values, 'expected':expected})


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot');parser.add_argument('operation',choices=['begin','checkpoint','finalize','stop'])
    parser.add_argument('--job-id',required=True);parser.add_argument('--attempt-id',required=True)
    parser.add_argument('--source-main');parser.add_argument('--automation-id');parser.add_argument('--checkpoint')
    parser.add_argument('--reason');parser.add_argument('--model',default='unconfirmed');parser.add_argument('--output',required=True)
    args=parser.parse_args()
    from risk_analysis_guard import strict_json
    snapshot=strict_json(Path(args.snapshot).read_text());queue=SnapshotQueue(snapshot)
    if not isinstance(snapshot.get('queue_head'),str) or not re.fullmatch(r'[0-9a-f]{40}',snapshot['queue_head']):
        raise ValueError('고정 queue_head 누락')
    if args.operation=='begin':value=begin(queue,args.job_id,args.attempt_id,args.source_main or '',args.automation_id)
    elif args.operation=='checkpoint':value=save_checkpoint(queue,args.job_id,args.attempt_id,strict_json(Path(args.checkpoint).read_text()))
    elif args.operation=='finalize':value=finalize(queue,args.job_id,args.attempt_id,args.model)
    else:value=stop(queue,args.job_id,args.attempt_id,args.reason)
    output={'queue_head':snapshot['queue_head'],'transactions':queue.transactions,'value':value,
            'scope':'offline_plan_publish_with_exact_head_lease_and_readback'}
    import os
    with open(args.output,'x') as stream:
        os.chmod(args.output,0o600);json.dump(output,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'operation':args.operation,'transactions':len(queue.transactions),'smtp_calls':0,'paid_ai_calls':0}))
