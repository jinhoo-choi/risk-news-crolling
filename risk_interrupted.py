"""Isolate expired interrupted rounds without changing receipts, status or ACK.

The shared risk-monitor Actions lock excludes an active sender. The original
TTL must also have elapsed. Immutable evidence reserves only the verified
source versions present in a possibly delivered packet, never all its inputs.
"""
import os

from bs4 import BeautifulSoup

from risk_outbox import validate_packet
from risk_partial_close import source_version
from risk_queue import JOB_ID, digest, now
from risk_result_validator import utc_time
from risk_shards import hydrate


STATES = {'sending', 'uncertain', 'delivery_complete'}


def isolate_expired(queue, small, job_sha, index, index_sha):
    jid = small['job_id']
    path = 'interruptions/'+jid+'.json'
    if (small.get('mode') != 'production' or small.get('self_test') is True
            or small.get('status') not in STATES
            or utc_time(small['expires_at']) > now()):
        raise RuntimeError('유효·실행 중 회차 격리 금지')
    entries = index.get('interrupted_jobs', {})
    if not isinstance(entries, dict):
        raise RuntimeError('중단 회차 색인 형식 오류')
    record, record_sha = queue.get(path)
    if jid in entries:
        if (not record or entries[jid] != digest(record)
                or record.get('source_job_sha') != job_sha
                or record.get('source_job_hash') != digest(small)
                or record.get('job_id') != jid or record.get('schema_version') != 1):
            raise RuntimeError('중단 회차 증거 변경 — 원본 보존')
        for directory, key in [('outbox', 'outbox_sha'), ('results', 'result_sha')]:
            if queue.get(directory+'/'+jid+'.json')[1] != record.get(key):
                raise RuntimeError('중단 회차 고정본 변경 — 원본 보존')
        return index, index_sha, set() if record['self_only'] else set(record['reserved_versions'])
    if record is not None:
        raise RuntimeError('중단 회차 기존 기록·색인 불일치')
    full = hydrate(queue, small)
    if (digest(full['input']) != small['input_hash']
            or digest(full['body_manifest']) != small['body_manifest_hash']):
        raise RuntimeError('중단 회차 입력·본문 해시 불일치')
    packet_path, result_path = 'outbox/'+jid+'.json', 'results/'+jid+'.json'
    packet, packet_sha = queue.get(packet_path)
    result, result_sha = queue.get(result_path)
    members, reported, versions = set(), set(), set()
    if packet is not None:
        validate_packet(packet, full, result)
        if small.get('review_evidence_version') == 1 and small.get('review_receipt') != {
                'attempt_id': result.get('review_attempt_id'), 'result_hash': digest(result)}:
            raise RuntimeError('중단 회차 분석 소유권 증거 불일치')
        hrefs = {a.get('href') for a in BeautifulSoup(packet['html'], 'html.parser').find_all('a')}
        reported = {a['article_id'] for a in full['input']['items'] if a['url'] in hrefs}
        # A staged packet without an SMTP reservation cannot have been sent.
        if small.get('receipts') or small.get('attempted_count', 0):
            members.update(reported)
            for group in result.get('event_groups', []):
                if group.get('reviewed') is True and reported.intersection(group['article_ids']):
                    members.update(group['article_ids'])
            held = set(small.get('deferred_ids', []))
            if reported.intersection(held):
                raise RuntimeError('중단 발송 고정본에 보류 기사 포함')
            bodies = {b['article_id']: b for b in full['body_manifest']}
            for aid in members-held:
                version = source_version(bodies.get(aid, {}))
                if version and result['analyses'].get(aid, {}).get('body_status') == 'verified':
                    versions.add(version)
                elif aid in reported:
                    raise RuntimeError('중단 발송 기사 원문 비교 불가')
    elif small.get('receipts') or small.get('attempted_count', 0):
        raise RuntimeError('전송 흔적이 있는 중단 회차 고정본 누락')
    record = {'schema_version': 1, 'job_id': jid, 'isolated_at': now().isoformat(),
              'source_job_sha': job_sha, 'source_job_hash': digest(small),
              'outbox_sha': packet_sha, 'result_sha': result_sha,
              'reported_ids': sorted(reported), 'reserved_versions': sorted(versions),
              'self_only': packet['self_only'] if packet else False,
              'status': small['status'], 'receipts_hash': digest(small.get('receipts', {})),
              'reason': 'expired_interrupted_round', 'smtp_calls': 0, 'history_writes': 0, 'ack': False}
    index.setdefault('interrupted_jobs', {})[jid] = digest(record)
    commit = queue.put_many({path: record, 'index.json': index},
                            {path: record_sha, 'index.json': index_sha, 'jobs/'+jid+'.json': job_sha,
                             packet_path: packet_sha, result_path: result_sha})
    saved, saved_sha = queue.get('index.json', ref=commit)
    if saved != index:
        raise RuntimeError('중단 회차 격리 저장 확인 실패')
    print('만료 중단 회차 격리 확인; 기존 영수증·상태·이력·ACK 보존/API0/SMTP0')
    return saved, saved_sha, set() if record['self_only'] else versions


def delivery_reservations(queue):
    """Self-only uncertainty must not suppress the first full-recipient report."""
    index, index_sha = queue.get('index.json')
    scopes = {'self': set(), 'full': set()}
    for jid in (index or {}).get('interrupted_jobs', {}):
        small, job_sha = queue.get('jobs/'+jid+'.json')
        if small and small.get('status') in {'completed', 'delivery_failed', 'closed_partial'}:
            continue
        isolate_expired(queue, small, job_sha, index, index_sha)
        record, _ = queue.get('interruptions/'+jid+'.json')
        scopes['self' if record['self_only'] else 'full'].update(record['reserved_versions'])
    return scopes


def isolate_job(queue, jid):
    """Explicit maintenance through the existing locked request workflow."""
    from risk_subscription import current_config
    if not JOB_ID.fullmatch(jid) or os.environ.get('GITHUB_REF') != 'refs/heads/main':
        raise RuntimeError('중단 회차 격리는 운영 main만 허용')
    current_config(queue)
    fresh = getattr(queue, 'get_fresh', queue.get)
    small, job_sha = fresh('jobs/'+jid+'.json')
    index, index_sha = fresh('index.json')
    if not small or small.get('job_id') != jid or not index or jid not in index['jobs']:
        raise RuntimeError('중단 회차·색인 누락')
    isolate_expired(queue, small, job_sha, index, index_sha)
    return {'job_id': jid, 'status': 'isolated', 'paid_ai_calls': 0, 'smtp_calls': 0,
            'history_writes': 0, 'ack': False}
