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
from risk_body_quality import body_quality

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


def validate_action_plan(analysis, source_text, action=None):
    """Check source-linked execution details, not length or a quality score.

    Semantic appropriateness and exposure suitability still need source review.
    This contract also runs on the final post-processed text before any SMTP.
    """
    plan = analysis.get('action_plan')
    if not isinstance(plan, dict):
        raise ValueError('대응방안 실행 근거 누락')
    action = analysis['action'] if action is None else action
    for field in ('event_anchor','current_task','check_source','followup_condition','followup_task'):
        value = plan.get(field)
        if not isinstance(value, str) or not value.strip() or value not in action:
            raise ValueError('대응방안 실행 항목 누락·최종 문구 불일치: '+field)
    if plan['event_anchor'] not in source_text:
        raise ValueError('대응방안 사건 근거가 원문에 없음')
    if not re.search(r'공시|거래소|법원|회사 발표|공식 안내|시세|주문 내역|체결 내역|장애 공지', plan['check_source']):
        raise ValueError('대응방안 대조 근거 불명확')
    if not re.search(r'면(?:\s|$)|경우|때|(?:결정|공지|공시|발표|변경|발생|확인|악화|중단|재개|확정)\s*시(?:\s|$)',
                     plan['followup_condition']):
        raise ValueError('대응방안 후속 조건 불명확')
    compact = lambda text: re.sub(r'\s+', '', text)
    vague = {'공시확인','상태대조','고객안내준비','영향점검','평가영향점검',
             '추가결정시','상황변경시','실제시장급락과당사관련현황을별도확인'}
    if (any(compact(plan[f]) in vague for f in ('current_task','followup_condition','followup_task'))
            or plan['current_task'] == plan['followup_task']):
        raise ValueError('대응방안 추상 조치·조건 반복')
    task = compact(plan['current_task'])
    if (re.search(r'평가변동|관련현황|평가영향', task)
            and not re.search(r'산출|재계산|재평가|분류|인계|미체결|담보부족|담보비율|청구내역|상환일정', task)):
        raise ValueError('대응방안 현재 처리 업무가 추상적임')


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
    if job.get('event_grouping_version') == 1 and 'event_groups' not in result:
        raise ValueError('동일 사건 전건 비교 결과 누락')
    grouped = set()
    for group in result.get('event_groups', []):
        members = set(group['article_ids'])
        if not members <= true_ids or members & grouped:
            raise ValueError('동일 사건 그룹의 미분석·중복 ID')
        analyses = [result['analyses'][aid] for aid in members]
        if any(a['body_status'] != 'verified' for a in analyses):
            raise ValueError('동일 사건 그룹 본문 미검증')
        if len({a['event_type'] for a in analyses}) != 1:
            raise ValueError('서로 다른 위험 유형의 동일 사건 병합 금지')
        grouped.update(members)
    for key, verdict in verdicts.items():
        if job.get('body_quality_version') == 1:
            status, reason = body_quality(bodies[key]['body'])
            if bodies[key]['body_status'] == 'verified' and status != 'verified':
                raise ValueError('본문 품질 미검증: ' + key + ': ' + reason)
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
        if not analysis['entity']:
            source_text = '\n'.join(segments.values())
            market = (analysis['event_type'] == '반대매매' and re.search(r'반대매매|강제\s*청산|마진콜', source_text))
            # Match within a sentence and allow line breaks/ordinary decline verbs.
            # A company's fall in another sentence must not turn a rising index into a market event.
            clauses = [clause for segment in segments.values()
                       for clause in re.split(r'(?<!\d)[.!?。,;](?!\d)|지만|으나|반면|한편|와 달리',
                                              re.sub(r'\s+', ' ', segment))]
            market = market or (analysis['event_type'] == '기타리스크' and any(
                re.search(r'서킷\s*브레이커|사이드\s*카|'
                          r'(?:코스피|코스닥).{0,100}(?:급락|폭락|하락|내렸|내린|내려|붕괴)|'
                          r'(?:급락|폭락|하락)(?:한|하는|세의)?\s*(?:코스피|코스닥)', clause)
                for clause in clauses))
            if not market or analysis['entities']:
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
        direct = analysis.get('direct_event_entities', [])
        if not set(direct) <= set(analysis['entities']):
            raise ValueError('직접 사건 주체는 entities에 포함되어야 함')
        if direct and analysis['body_status'] != 'verified':
            raise ValueError('직접 복수 주체 원문 미확인')
        for entity in direct:
            if not any(e['source_segment'] == 'body' and entity in e['quote']
                       for e in analysis['evidence']):
                raise ValueError('직접 사건 주체의 본문 인용 누락')
        allowed_numbers = numbers('\n'.join(segments.values()))
        for field in ('fact_summary', 'action', 'customer_notice'):
            text = analysis[field] or ''
            if numbers(text) - allowed_numbers:
                raise ValueError('원문에 없는 수치: ' + field)
        if (job.get('action_quality_version') == 1 and analysis['body_status'] == 'verified'
                and analysis['base_grade'] in ('긴급','주의')):
            validate_action_plan(analysis, '\n'.join(segments.values()))
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
