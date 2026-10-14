"""로컬 검수용 계약 초안. 운영 큐·SMTP·GitHub를 호출하지 않는다.

이 검사는 구조와 기계적으로 확인할 근거를 검사한다. 원문 주장 진위,
사건 귀속과 긴급성의 의미 판단까지 자동으로 증명하는 구현은 아니다.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA = json.loads(Path(__file__).with_name('risk_result_schema.json').read_text())
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def strict_json(raw):
    def pairs(entries):
        obj = {}
        for key, value in entries:
            if key in obj:
                raise ValueError('중복 JSON 키')
            obj[key] = value
        return obj

    def constant(value):
        raise ValueError('비유한 JSON 숫자')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def utc_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('시각의 시간대 누락')
    return parsed.astimezone(timezone.utc)


def assert_writable(config, job, current):
    if config.get('engine') != 'subscription':
        raise ValueError('구독 모드 아님')
    if job.get('status') != 'pending' or job.get('receipts') or job.get('attempted_count', 0):
        raise ValueError('미발송 pending 아님')
    if utc_time(job['expires_at']) <= current:
        raise ValueError('만료')
    if job.get('mode') not in ('verification', 'production'):
        raise ValueError('알 수 없는 회차 모드')


def safe_to_expire(job, current):
    return (job.get('status') == 'pending' and not job.get('receipts')
            and not job.get('attempted_count', 0) and utc_time(job['expires_at']) <= current)


def numbers(value):
    return {part.replace(',', '') for part in re.findall(r'\d+(?:[.,]\d+)*', value)}


def validate(result, job, inputs, manifest):
    # 비유한 수와 bool을 숫자로 받지 않는다. bool은 JSON Schema도 거부한다.
    json.dumps(result, allow_nan=False)
    VALIDATOR.validate(result)
    for field, expected in [('job_id', job['job_id']), ('input_hash', digest(inputs)),
                            ('policy_hash', job['policy_hash']),
                            ('body_manifest_hash', digest(manifest))]:
        if result[field] != expected:
            raise ValueError(field + ' 불일치')
    ids = [item['article_id'] for item in inputs['items']]
    if len(ids) != len(set(ids)):
        raise ValueError('입력 ID 중복')
    classified = [item['article_id'] for item in result['classifications']]
    if len(classified) != len(set(classified)) or set(classified) != set(ids):
        raise ValueError('전건 ID 계약 위반')
    body_ids = [item['article_id'] for item in manifest]
    if len(body_ids) != len(set(body_ids)) or set(body_ids) != set(ids):
        raise ValueError('본문 manifest ID 계약 위반')
    source = {item['article_id']: item for item in inputs['items']}
    bodies = {item['article_id']: item for item in manifest}
    verdicts = {item['article_id']: item['relevant'] for item in result['classifications']}
    true_ids = {key for key, verdict in verdicts.items() if verdict is True}
    if set(result['analyses']) != true_ids:
        raise ValueError('확정 후보 분석 누락·추가')
    for key, verdict in verdicts.items():
        visible = re.sub(r'\[(?:링크|URL)[^\]]*\]', '', bodies[key]['body'])
        if bodies[key]['body_status'] in ('verified', 'alternate_verified') and not any(c.isalnum() for c in visible):
            raise ValueError('본문 내용 없이 확보 완료 표시')
        if bodies[key]['body_status'] == 'alternate_verified':
            raise ValueError('대체 본문의 독립 출처 검수 미구현 — 발송 보류')
    for key, analysis in result['analyses'].items():
        article, body = source[key], bodies[key]
        segments = {'title': article['title'], 'description': article['description'], 'body': body['body']}
        if analysis['body_status'] != body['body_status'] and not (
                body['body_status'] == 'verified' and analysis['body_status'] in ('missing','unreadable')):
            raise ValueError('본문 상태 위조')
        if analysis['body_status'] in ('missing', 'unreadable'):
            if analysis['base_grade'] != '참고' or analysis['confidence'] > 0.6 or analysis['customer_notice']:
                raise ValueError('본문 미수집은 참고·낮은 확신도·고객 안내 없음')
            if '[본문 미수집]' not in analysis['fact_summary']:
                raise ValueError('본문 미수집 표시 누락')
            if any(e['source_segment'] == 'body' for e in analysis['evidence']):
                raise ValueError('미수집 본문을 근거로 삼음')
        if not math.isfinite(analysis['confidence']):
            raise ValueError('confidence 비유한 값')
        if not analysis['entity'] and (analysis['event_type'] != '반대매매' or analysis['entities']):
            raise ValueError('빈 주체를 허용한 시장 사건 아님')
        if analysis['entity'] == '한국투자증권':
            if not re.search(r'한국투자증권|(?<![가-힣])한투증권', '\n'.join(segments.values())):
                raise ValueError('당사 이름 근거 없음')
        if analysis['judgment']['당사연관'] == '당사이슈' and analysis['entity'] != '한국투자증권':
            raise ValueError('당사 사건의 행위 주체 불일치')
        for evidence in analysis['evidence']:
            segment = segments[evidence['source_segment']]
            if evidence['source_hash'] != text_digest(segment) or evidence['quote'] not in segment:
                raise ValueError('원문 해시·연속 인용 불일치')
        allowed_numbers = numbers('\n'.join(segments.values()))
        for field in ('fact_summary', 'action', 'customer_notice'):
            text = analysis[field] or ''
            if numbers(text) - allowed_numbers:
                raise ValueError('원문에 없는 수치: ' + field)
        if len((analysis['customer_notice'] or '').splitlines()) > 5:
            raise ValueError('고객 안내 줄 수 초과')
    return {'classifications': len(ids), 'analyses': len(true_ids),
            'needs_review': sum(value is None for value in verdicts.values()),
            'consume_ready': not any(value is None for value in verdicts.values())}


def assert_consume_ready(result, job, inputs, manifest, config, current):
    assert_writable(config, job, current)
    summary = validate(result, job, inputs, manifest)
    if not summary['consume_ready']:
        raise ValueError('확인 필요 입력이 남음')
    return summary
