"""Short, source-bound first-pass decisions; no model or network calls.

This proves the source and recorded decision, not a model's private reasoning.
Only the new frozen contract opts in; old receipts/results stay immutable.
"""
import hashlib
import re
from risk_body_quality import body_quality

NOT_REVIEWED = re.compile(r'미시도|미열람|시간\s*부족|아직\s*(?:읽|분석)|'
                         r'not[ _-](?:reviewed|attempted)|\bunread\b', re.I)


def source_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def unavailable_basis(body):
    return {'kind': 'unavailable', 'source_hash': source_hash(body['body'])}


def bind_sources(checkpoint, job):
    """Compute missing mechanical fields; never invent a verdict or quotation.

    The reviewer records kind/quote/blocker. Existing hashes and rule names are
    left intact so a mismatch fails validation instead of being repaired away.
    """
    import copy
    checkpoint = copy.deepcopy(checkpoint)
    articles = {a['article_id']: a for a in job['input']['items']}
    bodies = {b['article_id']: b for b in job['body_manifest']}
    for row in checkpoint['classifications']:
        basis = row.get('decision_basis')
        if not isinstance(basis, dict) or row['article_id'] not in articles:
            continue
        article, body = articles[row['article_id']], bodies[row['article_id']]
        if basis.get('kind') == 'metadata_rule':
            from naver_news_monitor import is_hard_excluded
            excluded, rule = is_hard_excluded(article['title'], article['description'], article['url'])
            if not excluded:
                raise ValueError('명확한 제외 규칙 불일치 — 본문 판단 필요: '+row['article_id'])
            basis.setdefault('rule', rule)
            basis.setdefault('source_hash', source_hash(article['title']+'\n'+article['description']))
        elif basis.get('kind') in {'body', 'unavailable'}:
            basis.setdefault('source_hash', source_hash(body['body']))
    return checkpoint


def validate_decision(row, article, body, analysis=None):
    """Missing first-pass proof is incomplete work, never a consumable hold."""
    if NOT_REVIEWED.search(str(row.get('reason', ''))):
        raise ValueError('미시도 판정을 null/false로 대체 금지: '+row['article_id'])
    basis = row.get('decision_basis')
    if row['relevant'] is True and basis is None and isinstance(analysis, dict):
        # A bare true + empty/missing analysis must not disguise unattempted work.
        # One valid source quote proves a recorded attempt; factual errors in
        # other analysis fields still quarantine only this individual article.
        for evidence in analysis.get('evidence', []) if isinstance(analysis.get('evidence'), list) else []:
            if (isinstance(evidence, dict) and evidence.get('source_segment') == 'body'
                    and evidence.get('source_hash') == source_hash(body['body'])
                    and isinstance(evidence.get('quote'), str) and len(evidence['quote'].strip()) >= 8
                    and evidence['quote'] in body['body']):
                return
    if not isinstance(basis, dict):
        raise ValueError('1차 판단 근거 누락 — 본문 확인 또는 확정 제외 규칙 필요: '+row['article_id'])
    kind = basis.get('kind')
    if kind == 'unavailable':
        if (set(basis) != {'kind', 'source_hash'} or row['relevant'] is not None
                or basis['source_hash'] != source_hash(body['body'])
                or body['body_status'] == 'verified' and body_quality(body['body'])[0] == 'verified'):
            raise ValueError('본문 미확보 상태 근거 불일치: '+row['article_id'])
        return
    if kind == 'metadata_rule':
        # Reuse the actual, unchanged deterministic exclusion rules. A generic
        # title such as "service status" never qualifies as an exclusion rule.
        from naver_news_monitor import is_hard_excluded
        text = article['title']+'\n'+article['description']
        excluded, rule = is_hard_excluded(article['title'], article['description'], article['url'])
        if (set(basis) != {'kind', 'source_hash', 'rule'} or row['relevant'] is not False
                or not excluded or basis['rule'] != rule or basis['source_hash'] != source_hash(text)):
            raise ValueError('명확한 제외 규칙 불일치 — 본문 판단 필요: '+row['article_id'])
        return
    if kind != 'body' or not {'kind', 'source_hash', 'quote'} <= basis.keys() or set(basis)-{'kind', 'source_hash', 'quote', 'blocker'}:
        raise ValueError('1차 판단 근거 형식 오류: '+row['article_id'])
    quote = basis['quote']
    if (basis['source_hash'] != source_hash(body['body']) or not isinstance(quote, str)
            or not 8 <= len(quote.strip()) <= 240 or quote not in body['body']):
        raise ValueError('1차 판단 본문 인용·해시 불일치: '+row['article_id'])
    if row['relevant'] is None and basis.get('blocker') not in {
            'entity', 'event_stage', 'date', 'conflicting_evidence', 'unrelated_body', 'analysis_failed'}:
        raise ValueError('실제 시도 후 보류 사유 누락: '+row['article_id'])
    if row['relevant'] is not None and 'blocker' in basis:
        raise ValueError('보류 사유를 제외로 처리 금지: '+row['article_id'])
