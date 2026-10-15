"""Immutable private mail/history plans. No SMTP, inference, or live prices."""
import copy
import hashlib
import json
import re
import tempfile
import unicodedata
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from risk_analysis_guard import strict_json, validate_seen_data
from risk_delivery import address_key
from risk_queue import digest, now


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def make_packet(job, result, subject, document, targets, self_only, seen):
    value = {'schema_version': 1, 'job_id': job['job_id'],
             'input_hash': job['input_hash'], 'policy_hash': job['policy_hash'],
             'body_manifest_hash': job['body_manifest_hash'], 'result_hash': digest(result),
             'source_history_hash': job['input']['history_hash'],
             'expected_receipts': [address_key(t) for t in targets],
             'subject': subject, 'html': document, 'self_only': self_only,
             'self_test': job['mode'] == 'verification', 'paid_ai_calls': 0,
             'seen_after_snapshot': seen, 'seen_after_hash': sha(seen),
             'created_at': now().isoformat()}
    value['packet_hash'] = digest(value)
    validate_packet(value, job, result)
    return value


def validate_packet(value, job, result):
    if not isinstance(value, dict):
        raise RuntimeError('비공개 발송 고정본 없음')
    unsigned = {k: v for k, v in value.items() if k != 'packet_hash'}
    if (value.get('schema_version') != 1 or value.get('packet_hash') != digest(unsigned)
            or any(value.get(k) != job[k] for k in ('job_id', 'input_hash', 'policy_hash', 'body_manifest_hash'))
            or value.get('result_hash') != digest(result)
            or value.get('source_history_hash') != job['input']['history_hash']
            or value.get('self_test') != (job['mode'] == 'verification')
            or value.get('paid_ai_calls') != 0 or type(value.get('self_only')) is not bool):
        raise RuntimeError('발송 고정본 회차·해시 불일치')
    targets = value.get('expected_receipts')
    if (not isinstance(targets, list) or not targets or len(targets) != len(set(targets))
            or any(not isinstance(t, str) or not re.fullmatch(r'[0-9a-f]{64}', t) for t in targets)
            or (job.get('expected_receipts') and targets != job['expected_receipts'])):
        raise RuntimeError('발송 고정본 수신자 불일치')
    if (not isinstance(value.get('subject'), str) or not value['subject']
            or '\r' in value['subject'] or '\n' in value['subject']
            or not isinstance(value.get('html'), str) or not value['html']
            or not isinstance(value.get('seen_after_snapshot'), str)
            or sha(value['seen_after_snapshot']) != value.get('seen_after_hash')):
        raise RuntimeError('발송 고정본 본문·이력 형식 오류')
    after = validate_seen_data(strict_json(value['seen_after_snapshot']))
    seen_urls = {u for k, entry in after.items() if k != '_event_history'
                 for u in (entry if isinstance(entry, list) else entry.get('urls', []))}
    held = {r['article_id'] for r in result['classifications'] if r['relevant'] is None}
    if any(a['url'] in seen_urls for a in job['input']['items'] if a['article_id'] in held):
        raise RuntimeError('보류 URL 처리 이력 저장 금지')
    return value


def store_packet(queue, value):
    path = 'outbox/'+value['job_id']+'.json'
    existing, _ = queue.get(path)
    if existing is not None:
        if existing != value:
            raise RuntimeError('기존 발송 고정본 덮어쓰기 금지')
        return
    queue.put(path, value)
    if getattr(queue, 'get_fresh', queue.get)(path)[0] != value:
        raise RuntimeError('발송 고정본 저장 확인 실패 — SMTP 금지')


def legacy_packet(queue, job, result, bot, targets):
    """Explicit operator adoption of a pre-outbox partial delivery, never re-render.

    Only the articles actually linked in the saved/Gmail-checked HTML enter
    delivery history. No old non-delivered article is silently marked seen.
    """
    request, _ = queue.get('recoveries/'+job['job_id']+'.json')
    preview, _ = queue.get('previews/'+job['job_id']+'.json')
    if (not request or not preview or request.get('schema_version') != 1
            or request.get('job_id') != job['job_id']
            or request.get('job_hash') != digest(job)
            or request.get('result_hash') != digest(result)
            or request.get('preview_hash') != digest(preview)
            or request.get('gmail_html_sha256') != sha(preview.get('html', ''))
            or request.get('confirmed_accepted_keys') != sorted(job.get('receipts', {}))
            or len(request.get('gmail_message_ids', [])) != len(job.get('receipts', {}))
            or preview.get('self_test') is not False):
        raise RuntimeError('기존 부분 발송의 Gmail·원본·운영자 검증 자료 없음')
    hrefs = {a.get('href') for a in BeautifulSoup(preview['html'], 'html.parser').find_all('a')}
    rows = [a for a in job['input']['items'] if a['url'] in hrefs]
    ids = [a['article_id'] for a in rows]
    if (not ids or ids != request.get('delivered_article_ids')
            or any(aid not in result['analyses'] for aid in ids)
            or [address_key(t) for t in targets] != job['expected_receipts']):
        raise RuntimeError('기존 메일 기사·수신자 검증 실패')
    before = Path('seen_news.json').read_bytes()
    if hashlib.sha256(before).hexdigest() != job['input']['history_hash']:
        raise RuntimeError('부분 발송 이후 운영 이력 변경 — 자동 합치기 금지')
    with tempfile.TemporaryDirectory() as directory:
        seen_file = Path(directory)/'seen_news.json'
        seen_file.write_bytes(before)
        with patch.object(bot, 'SEEN_FILE', str(seen_file)):
            ledger = bot.load_event_history()
            canon = bot.load_entity_canonical_map()
            combos, stages, titles, descriptions = set(), set(), [], []
            def norm(text):
                text = re.sub(r'\[.*?\]|\(.*?\)', '', unicodedata.normalize('NFKC', text or ''))
                return re.sub(r'[^가-힣a-zA-Z0-9]', '', text).strip()
            for row in rows:
                analysis = result['analyses'][row['article_id']]
                entity = bot.canonicalize_entity(analysis['entity'], canon)
                event = analysis['event_type']
                title, description = row['title'], row.get('desc', row.get('description', ''))
                ledger.record(entity, title, description, event, scope='full')
                if entity and event:
                    combos.update({('ek', entity+'_'+event), (entity, event)})
                if entity:
                    stages.update((entity, f['stage']) for f in bot.material_facts(title, description, now=ledger.now))
                titles.append(norm(title)); descriptions.append(norm(description))
            bot.save_seen_urls({a['url'] for a in rows}, combos,
                               title_norms=titles, desc_norms=descriptions,
                               stages=stages, event_history=ledger)
        seen = seen_file.read_text()
    value = make_packet(job, result, request['subject'], preview['html'], targets, False, seen)
    value.pop('packet_hash')
    value.update(legacy_adoption=True, delivered_article_ids=ids,
                 history_reconstruction='only_saved_mail_articles', adoption_hash=digest(request))
    value['packet_hash'] = digest(value)
    return validate_packet(value, job, result)
