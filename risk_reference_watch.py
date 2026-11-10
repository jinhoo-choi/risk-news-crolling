"""Source-bound reference alerts for new subscription rounds only."""
import hashlib
import re


OWNER = '한국투자증권'
INDUSTRY = '증권업계'
OWNER_ALIASES = r'한국투자증권|한투증권|한투證|뱅키스'


def reference_search_candidate(title, description=''):
    """Recall only: a metadata match still needs source-bound body review."""
    text = re.sub(r'\s+', '', title+' '+(description or ''))
    return bool(re.search(r'금융감독원|금감원|금융위원회|금융위', text)
                and re.search(OWNER_ALIASES+r'|증권사|증권업계', text)
                and re.search(r'점검|검사|제재.{0,8}예고|사전통지|제도개선|개선방안|영업관행', text))


def reference_kind(entity, event_type, judgment, quotes):
    if event_type != '기타리스크' or entity not in (OWNER, INDUSTRY):
        return ''
    core = judgment.get('핵심사건', '')
    clauses = [re.sub(r'\s+', '', clause) for q in quotes
               for clause in re.split(r'(?<!\d)[.!?。;](?!\d)', q)]
    if entity == OWNER and re.search(r'전화|상담|접수|신청', core):
        for clause in clauses:
            if not re.search(OWNER_ALIASES, clause):
                continue
            if re.search(r'예방|훈련|원활|사실무근|문제(?:가)?없|불편없|해소했|정상화됐', clause):
                continue
            if (re.search(r'전화|상담|접수|신청|통화|고객센터', clause)
                    and re.search(r'불통|대기|끊겼|끊긴|끊겨|연결.{0,15}(?:실패|않|어렵|불가)|'
                                  r'접수.{0,15}(?:지연|실패|불가)', clause)):
                return 'owner_service'
    if re.search(r'감독|점검|검사|제재.{0,4}예고|개선|사전통지', core):
        for clause in clauses:
            if not re.search(r'금융감독원|금감원|금융위원회|금융위', clause):
                continue
            if re.search(r'가능성|관측|추측|검토해야|필요하다|사실무근|계획(?:이)?없|예고하지않', clause):
                continue
            target = OWNER_ALIASES if entity == OWNER else r'증권사|증권업계'
            if (re.search(target, clause)
                    and re.search(r'점검|검사|개선방안|개선하기로|제재.{0,8}예고|'
                                  r'사전통지|제도개선|영업관행', clause)
                    and re.search(r'밝혔|발표|마련|통보|통지했|예고했|예고했다|추진|착수|하기로', clause)):
                return 'supervisory_notice'
    return ''


def verified_reference_watch(article):
    body = article.get('body') or ''
    quotes = article.get('_reference_watch_quotes') or []
    kind = article.get('_subscription_reference_watch')
    return bool(kind and article.get('_body_risk_confirmed') is True
                and not article.get('_body_failed') and body and quotes
                and article.get('_body_source_url') == article.get('url')
                and article.get('_body_source_hash') == hashlib.sha256(body.encode()).hexdigest()
                and all(isinstance(q, str) and q in body for q in quotes)
                and reference_kind(article.get('entity'), article.get('event_type'),
                                   article.get('_judgment') or {}, quotes) == kind)
