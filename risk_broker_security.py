"""Source evidence for the subscription-only broker security exception."""
import hashlib
import re


def confirmed_security_evidence(entity, quotes):
    if not entity:
        return False
    name = re.sub(r'\s+', '', entity)
    for quote in quotes:
        text = re.sub(r'\s+', '', quote)
        for clause in re.split(r'(?<!\d)[.!?。;](?!\d)', text):
            if name not in clause:
                continue
            if re.search(r'의혹|가능성|우려|예방|모의|훈련|사실무근|확인되지|발생하지|유출되지|피해없|피해가없', clause):
                continue
            if (re.search(r'해킹|침해사고|보안사고|(?:개인(?:\(신용\))?|고객|임직원|신용)정보.{0,30}유출', clause)
                    and re.search(r'발생|확인했|확인됐|확인되었|유출됐|유출되었|유출됐다|유출사실|피해를입|피해가발생|공격을받|당했|사과|인정했', clause)):
                return True
    return False


def verified_broker_security(article):
    body = article.get('body') or ''
    quotes = article.get('_broker_security_quotes') or []
    return bool(
        article.get('_subscription_broker_security')
        and article.get('_body_risk_confirmed') is True
        and not article.get('_body_failed')
        and body
        and article.get('_body_source_url') == article.get('url')
        and article.get('_body_source_hash') == hashlib.sha256(body.encode()).hexdigest()
        and article.get('_judgment', {}).get('확정여부') == '확정'
        and quotes
        and all(isinstance(quote, str) and quote in body for quote in quotes)
        and confirmed_security_evidence(article.get('entity'), quotes)
    )
