"""Pure validation for risk analysis, history and direct incident evidence.

No LLM, network, SMTP or recipient selection lives in this module.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from event_history import HISTORY_KEY, _event_dates, _compact

KST = timezone(timedelta(hours=9))


class AnalysisIncompleteError(RuntimeError):
    """A failed or incomplete batch cannot establish a no-risk result."""


class HistoryIntegrityError(RuntimeError):
    """Do not replace unreadable delivery history with an empty ledger."""


def strict_json(text):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError('non-finite JSON number: ' + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def validate_grade_map(grades, batch, offset=0):
    if not isinstance(grades, list):
        raise AnalysisIncompleteError('AI 판정 배열 형식 오류')
    expected = set(range(offset + 1, offset + len(batch) + 1))
    mapped = {}
    for entry in grades:
        if not isinstance(entry, dict):
            raise AnalysisIncompleteError('AI 판정 객체 형식 오류')
        identifier = entry.get('id', entry.get('news_id'))
        if type(identifier) is not int or identifier not in expected or identifier in mapped:
            raise AnalysisIncompleteError('AI 판정 ID 누락·중복·범위 오류')
        if 'id' in entry and 'news_id' in entry and entry['id'] != entry['news_id']:
            raise AnalysisIncompleteError('AI 판정 ID 별칭 불일치')
        if type(entry.get('relevant')) is not bool:
            raise AnalysisIncompleteError('AI relevant는 boolean이어야 함')
        if entry['relevant']:
            if entry.get('grade') not in ('긴급', '주의', '참고'):
                raise AnalysisIncompleteError('AI 판정 등급 오류')
            confidence = entry.get('confidence')
            if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise AnalysisIncompleteError('AI confidence 형식·범위 오류')
            if not isinstance(entry.get('entity'), str):
                raise AnalysisIncompleteError('AI 주체 형식 오류')
            entities = entry.get('entities') or []
            if not isinstance(entities, list) or any(not isinstance(e, str) for e in entities):
                raise AnalysisIncompleteError('AI 복수 주체 형식 오류')
            if not entry['entity'].strip():
                article = batch[identifier - offset - 1]
                source = article.get('title', '') + ' ' + article.get('desc', '')
                if article.get('_body_risk_confirmed') is True and not article.get('_body_failed'):
                    body = article.get('body') or ''
                    if article.get('_body_source_url') == article.get('url') and article.get('_body_source_hash') == hashlib.sha256(body.encode()).hexdigest():
                        source += ' ' + body
                market_event = (entry.get('event_type') == '반대매매'
                                and re.search(r'반대매매|강제\s*청산|마진콜', source))
                market_event = market_event or (entry.get('event_type') == '기타리스크'
                                                and re.search(r'서킷\s*브레이커|사이드\s*카|코스피.*(?:급락|폭락)|코스닥.*(?:급락|폭락)', source))
                if not market_event or any(e.strip() for e in entities):
                    raise AnalysisIncompleteError('빈 주체의 시장 사건 원문 근거 없음')
            for field in ('reason', 'action', 'event_type'):
                if field == 'action' and entry.get(field) is None:
                    continue  # Legacy filters emit null when no action is needed.
                if field in entry and not isinstance(entry[field], str):
                    raise AnalysisIncompleteError('AI 문구 형식 오류: ' + field)
        mapped[identifier] = entry
    if set(mapped) != expected:
        raise AnalysisIncompleteError('AI 판정 ID 전건 완전성 오류')
    return mapped


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_seen_data(data):
    if not isinstance(data, dict):
        raise HistoryIntegrityError('발송 이력 최상위 객체 형식 오류')
    for key, slot in data.items():
        if key == HISTORY_KEY:
            if not isinstance(slot, dict) or slot.get('version') != 1 or not isinstance(slot.get('records'), list):
                raise HistoryIntegrityError('장기 사건 이력 형식 오류')
            for record in slot['records']:
                try:
                    if not isinstance(record, dict) or record.get('scope') not in ('self', 'full'):
                        raise ValueError('scope')
                    if not all(isinstance(record.get(f), str) and record[f] for f in ('entity', 'family')):
                        raise ValueError('entity/family')
                    first, last = (datetime.fromisoformat(record[f]) for f in ('first_sent', 'last_sent'))
                    if first.tzinfo is None or last.tzinfo is None or first > last:
                        raise ValueError('timestamps')
                    if not _strings(record.get('titles', [])) or not isinstance(record.get('facts'), list):
                        raise ValueError('facts/titles')
                    for fact in record['facts']:
                        if not isinstance(fact, dict) or not isinstance(fact.get('stage'), str):
                            raise ValueError('fact')
                        if not all(_strings(fact.get(f)) for f in ('amounts', 'dates')):
                            raise ValueError('fact lists')
                        if 'identities' in fact and not _strings(fact['identities']):
                            raise ValueError('fact identities')
                except (KeyError, TypeError, ValueError) as error:
                    raise HistoryIntegrityError('장기 사건 이력 레코드 손상') from error
            continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}', key):
            raise HistoryIntegrityError('발송 이력 시간 슬롯 형식 오류')
        if isinstance(slot, list):  # Per-hour legacy URL slots remain supported.
            if not _strings(slot):
                raise HistoryIntegrityError('구형 URL 이력 형식 오류')
            continue
        if not isinstance(slot, dict):
            raise HistoryIntegrityError('발송 이력 슬롯 손상')
        for field in ('urls', 'title_norms', 'desc_norms'):
            if field in slot and not _strings(slot[field]):
                raise HistoryIntegrityError('발송 이력 목록 손상: ' + field)
        for field in ('combos', 'stages'):
            values = slot.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, (list, tuple)) or len(v) != 2
                                                  or not all(isinstance(x, str) for x in v) for v in values):
                raise HistoryIntegrityError('발송 이력 쌍 목록 손상: ' + field)
    return data


def read_seen_data(path, allow_missing=False):
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError as error:
        if allow_missing:
            return {}
        raise HistoryIntegrityError('발송 이력 파일 없음') from error
    except OSError as error:
        raise HistoryIntegrityError('발송 이력 읽기 실패') from error
    try:
        return validate_seen_data(strict_json(text))
    except (ValueError, TypeError) as error:
        raise HistoryIntegrityError('발송 이력 JSON 손상') from error


def model_label(model, engine=None):
    model = model if isinstance(model, str) else ''
    if engine == 'chatgpt_subscription':
        return 'ChatGPT 구독 · 모델 미확인' if not model or model == 'unconfirmed' else 'ChatGPT 구독 · ' + model
    match = re.fullmatch(r'claude-(opus|sonnet|haiku)-[\w.-]+', model)
    if match:
        return 'Claude ' + match.group(1).capitalize()
    return 'AI 모델 미확인' if not model or model == 'unconfirmed' else 'AI · ' + model


_KIS = re.compile(r'한국투자증권|(?<![가-힣])한투증권')
_INCIDENT = re.compile(r'장애|오류|먹통|전산사고|서비스\s*중단|과태료|과징금|제재')
_NOT_CURRENT = re.compile(r'상반기|지난해|작년|과거|회고|당시|재발\s*방지|예정|가능성|우려|모의|가정')
_NEGATED = re.compile(r'(?:추가\s*)?(?:장애|오류|사고).{0,15}(?:없|않|아니)|문제.{0,8}없|(?:정상|복구)(?:화|됐|되었|됐다|되었다)')
_OTHER_BROKER = re.compile(r'키움증권|미래에셋증권|삼성증권|신한투자증권|대한투자증권|NH투자증권|KB증권')


def verified_direct_incident(article, now=None):
    """Return evidence only for a recent, body-verified KIS incident.

    No inferred event date is created. Missing dates or source provenance cannot
    unlock a title-based downgrade. A retrospective stays outside this exception.
    """
    now = now or datetime.now(KST)
    body = article.get('body') or ''
    judgment = article.get('_judgment') or {}
    if (not isinstance(body, str) or article.get('_body_failed') or article.get('_body_risk_confirmed') is not True
            or article.get('entity') not in ('한국투자증권', '한투증권')
            or judgment.get('당사연관') != '당사이슈' or judgment.get('확정여부') != '확정'
            or not article.get('url') or article.get('_body_source_url') != article['url']
            or article.get('_body_source_hash') != hashlib.sha256(body.encode()).hexdigest()):
        return None
    title = article.get('title') or ''
    if _NOT_CURRENT.search(title):
        return None
    if _OTHER_BROKER.search(title) and not _KIS.search(title):
        return None
    for paragraph in re.split(r'(?<=[다요])\.\s*|\n', body):
        company = _KIS.search(paragraph)
        if not company or not _INCIDENT.search(paragraph[company.end():company.end()+180]):
            continue
        if _NOT_CURRENT.search(paragraph) or _NEGATED.search(paragraph) or _OTHER_BROKER.search(paragraph):
            continue
        dates, old = _event_dates(_compact(paragraph), now)
        lower = (now - timedelta(hours=14)).date()
        if old or not dates or not any(lower <= datetime.fromisoformat(d).date() <= now.date() for d in dates):
            continue
        quote = paragraph.strip()
        if 8 <= len(quote) <= 400:
            return {'quote':quote, 'source_hash':article['_body_source_hash'],
                    'event_dates':dates, 'verified_at':now.isoformat()}
    return None


def apply_verified_direct_incident(article, now=None):
    previous = article.pop('_verified_direct_incident', None)
    evidence = verified_direct_incident(article, now)
    if evidence is None:
        if previous:
            article.update(grade='참고', _force_urgent=False, _grade_locked=True)
        # Clear the title-only urgent exception when verified source evidence is
        # wholly retrospective. A mixed or undated account is not assumed old.
        body = article.get('body') or ''
        judgment = article.get('_judgment') or {}
        source_ready = (isinstance(body, str) and not article.get('_body_failed')
                        and article.get('_body_risk_confirmed') is True
                        and article.get('entity') in ('한국투자증권', '한투증권')
                        and judgment.get('당사연관') == '당사이슈'
                        and article.get('url') and article.get('_body_source_url') == article['url']
                        and article.get('_body_source_hash') == hashlib.sha256(body.encode()).hexdigest())
        if source_ready:
            states = []
            for paragraph in re.split(r'(?<=[다요])\.\s*|\n', body):
                company = _KIS.search(paragraph)
                if company and _INCIDENT.search(paragraph[company.end():company.end()+180]) and not _NEGATED.search(paragraph):
                    _, old = _event_dates(_compact(paragraph), now or datetime.now(KST))
                    states.append(old)
            if states and all(states):
                article.update(grade='참고', _force_urgent=False, _grade_locked=True,
                               _verified_direct_recap=True, customer_notice=None)
                article.pop('_verified_direct_incident', None)
        return False
    article.update(entity='한국투자증권', grade='긴급', _force_urgent=True,
                   _verified_direct_incident=evidence, customer_notice=None)
    article.pop('_verified_direct_recap', None)
    article['entities'] = list(dict.fromkeys(['한국투자증권'] + (article.get('entities') or [])))
    article.pop('_grade_locked', None)
    return True
