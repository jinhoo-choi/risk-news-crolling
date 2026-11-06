"""Operator-approved partial close. No transport, public history update, or delivery ACK."""
import re

from bs4 import BeautifulSoup

from risk_outbox import validate_packet, sha
from risk_queue import JOB_ID, digest, now
from risk_result_validator import validate
from risk_shards import hydrate


def source_version(body):
    # Older collectors could silently clip at exactly 12,000 characters.
    text = body.get('body', '')
    if (body.get('body_status') != 'verified' or body.get('truncated') or not text
            or len(text) >= 12000 or not body.get('source_url')):
        return None
    return digest([body['article_id'], body['source_url'], text])


def checked_closure(job, index):
    record = job.get('partial_closure')
    if (job.get('mode') != 'production' or job.get('status') != 'closed_partial'
            or not isinstance(record, dict) or job.get('ack') or job.get('history_saved')
            or index.get('partial_closures', {}).get(job['job_id']) != digest(record)
            or record.get('receipts_hash') != digest(job['receipts'])
            or record.get('expected_hash') != digest(job['expected_receipts'])
            or record.get('attempted_count') != job['attempted_count']
            or not record.get('reported_ids') or not record.get('versions')
            or not all(re.fullmatch(r'[0-9a-f]{64}', key) for key in record['versions'])):
        raise RuntimeError('부분 발송 종결 증거·색인 불일치')
    return record


def close_partial(queue, jid, request_id):
    from risk_subscription import current_config, policy
    if not JOB_ID.fullmatch(jid) or not JOB_ID.fullmatch(request_id):
        raise ValueError('부분 종결 요청 ID 오류')
    current_config(queue)
    path = 'jobs/' + jid + '.json'
    fresh = getattr(queue, 'get_fresh', queue.get)
    small, job_sha = fresh(path)
    index, index_sha = fresh('index.json')
    if not small or not index or small.get('job_id') != jid:
        raise RuntimeError('부분 종결 회차·색인 누락')
    if small.get('status') == 'closed_partial':
        if checked_closure(small, index)['request_id'] != request_id:
            raise RuntimeError('다른 부분 종결 요청')
        return  # An exact replay is read-only even after later collections.
    auth_path = 'partial-close-requests/' + jid + '.json'
    auth, auth_sha = fresh(auth_path)
    if (not auth or auth.get('operation') != 'close_without_resend'
            or auth.get('job_id') != jid or auth.get('request_id') != request_id
            or auth.get('job_sha') != job_sha or index['jobs'][-1:] != [jid]
            or jid in index.get('partial_closures', {}) or small.get('mode') != 'production'
            or small.get('status') != 'uncertain' or small.get('self_test') is not False
            or small.get('history_saved') or small.get('ack') or small.get('paid_ai_calls') != 0):
        raise RuntimeError('부분 종결 승인·회차 상태 불일치')
    receipts = small['receipts']
    expected = small['expected_receipts']
    accepted = {k for k, v in receipts.items() if v == 'accepted'}
    proofs = auth.get('accepted_sent_messages', {})
    if (not accepted or not isinstance(proofs, dict) or set(proofs) != accepted
            or not all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{10,40}', v) for v in proofs.values())
            or len(set(proofs.values())) != len(accepted) or len(accepted) >= len(expected)
            or not set(receipts) <= set(expected) or len(expected) != len(set(expected))
            or not set(receipts.values()) <= {'accepted', 'uncertain', 'rejected'}
            or small['attempted_count'] != len(receipts)):
        raise RuntimeError('부분 종결 수신자 접수·발신본 불일치')
    result_path, packet_path = 'results/'+jid+'.json', 'outbox/'+jid+'.json'
    result, result_sha = fresh(result_path)
    packet, packet_sha = fresh(packet_path)
    if (not result or not packet or result_sha != auth.get('result_sha')
            or packet_sha != auth.get('outbox_sha') or packet.get('self_only') is not False
            or sha(packet['html']) != auth.get('sent_html_hash')):
        raise RuntimeError('부분 종결 승인 고정본 불일치')
    job = hydrate(queue, small)
    validate(result, job, job['input'], job['body_manifest'])
    if (job['input_hash'] != result['input_hash'] or job['body_manifest_hash'] != result['body_manifest_hash']
            or digest(policy()) != job['policy_hash']):
        raise RuntimeError('부분 종결 입력·본문·정책 변경')
    validate_packet(packet, job, result)
    hrefs = {a.get('href') for a in BeautifulSoup(packet['html'], 'html.parser').find_all('a')}
    reported = {a['article_id'] for a in job['input']['items'] if a['url'] in hrefs}
    if not reported or sorted(reported) != auth.get('reported_article_ids'):
        raise RuntimeError('발신본에 실린 기사와 승인 범위 불일치')
    members = set(reported)
    for group in result.get('event_groups', []):
        if reported.intersection(group['article_ids']) and group['reviewed'] is True:
            members.update(group['article_ids'])
    if members.intersection(small.get('deferred_ids', [])) or any(
            result['analyses'].get(i, {}).get('body_status') != 'verified' for i in members):
        raise RuntimeError('보류·미검증 기사 중복 제외 금지')
    versions = {b['article_id']: source_version(b) for b in job['body_manifest'] if b['article_id'] in members}
    if not all(versions.get(i) for i in reported):
        raise RuntimeError('대표 기사 원문 비교 불가')
    close = {'request_id': request_id, 'source_job_sha': job_sha, 'outbox_sha': packet_sha,
             'result_sha': result_sha, 'approval_sha': auth_sha, 'closed_at': now().isoformat(),
             'reported_ids': sorted(reported), 'matching_event_ids': sorted(members),
             'versions': sorted(v for v in versions.values() if v),
             'expected_hash': digest(expected), 'receipts_hash': digest(receipts),
             'attempted_count': small['attempted_count']}
    small.update(status='closed_partial', partial_closure=close)
    index.setdefault('partial_closures', {})[jid] = digest(close)
    checked_closure(small, index)
    queue.put_many({path: small, 'index.json': index},
                   {path: job_sha, 'index.json': index_sha, result_path: result_sha,
                    packet_path: packet_sha, auth_path: auth_sha})
