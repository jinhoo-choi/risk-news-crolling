"""Source-evidence event dedup, independent of the 72-hour article cache.

No model calls or dependencies. A changed model label/publication time is not a
new event. Only concrete source-text stages, amounts or event dates can reopen a
known event. Histories describe delivered cards, separately for self/full mail.
"""
from datetime import datetime, timedelta, timezone
import re
from decimal import Decimal
from difflib import SequenceMatcher
import unicodedata

HISTORY_KEY = "_event_history"
RETENTION_DAYS = 180
MAX_RECORDS = 2000
MAX_FACTS = 80
KST = timezone(timedelta(hours=9))


def _compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


# Related listing-risk labels often drift between runs. Other risk families stay
# separate, so a known listing case does not silence a new fraud/default story.
_FAMILIES = {
    "listing": r"상장폐지|상폐|거래정지|매매거래정지|감사의견|의견거절|정리매매|가처분|효력정지|집행정지|이의신청|항고",
    "insolvency": r"파산|회생|법정관리|워크아웃|청산절차|부도|디폴트",
    "sanction": r"과징금|과태료|제재|검찰고발|직무정지|영업정지",
    "crime": r"횡령|배임|기소|구속|압수수색|수사착수",
    "liquidity": r"미상환|미지급|연체|상환불능|지급불능|차환실패|유동성|자금난|주식매수청구권",
    "credit": r"신용등급|등급강등",
    "operations": r"영업중단|공장가동중단|생산중단|대규모리콜|중대재해",
}

# Stage patterns express an action/outcome, not a background noun such as
# '가처분 계류' or '2년 연속 감사의견 거절로 상폐 문턱에 선'.
_STAGES = [
    ("listing", "가처분기각", r"(?:가처분|효력정지|집행정지|이의신청|항고).{0,18}?(?:기각|각하)"),
    ("listing", "가처분신청", r"(?:가처분|효력정지|집행정지).{0,10}?신청"),
    ("listing", "상장폐지결정", r"(?:상장폐지|상폐).{0,5}?(?:결정|확정|효력발생)"),
    ("listing", "상장폐지사유", r"(?:상장폐지|상폐)(?:기준|사유).{0,8}?(?:해당|발생|통보)"),
    ("listing", "정리매매개시", r"정리매매.{0,6}?(?:개시|재개|돌입|시작|첫날)"),
    ("listing", "거래정지", r"(?:매매)?거래정지(?:결정|통보|시행|돌입|조치)?"),
    ("listing", "감사의견거절", r"(?:감사의견|의견)[\"'‘’“”]*(?:거절|부적정)"),
    ("insolvency", "파산선고", r"파산선고|부도(?:처리|발생|확정)|디폴트(?:발생|선언)"),
    ("insolvency", "회생신청", r"(?:기업)?회생(?:절차)?신청|법정관리신청"),
    ("insolvency", "회생개시", r"회생(?:절차)?.{0,5}?개시"),
    ("insolvency", "회생폐지", r"회생(?:절차)?.{0,5}?(?:폐지|기각)"),
    ("insolvency", "회생인가", r"회생계획.{0,5}?인가|회생인가"),
    ("insolvency", "청산절차", r"청산(?:절차|신청)|폐업(?:결정|신고)"),
    ("insolvency", "워크아웃개시", r"워크아웃(?:개시|신청|확정)"),
    ("sanction", "과징금", r"과징금|추가제재|과태료"),
    ("sanction", "검찰고발", r"검찰고발|검찰에.{0,8}?고발"),
    ("sanction", "영업정지", r"(?:영업|직무)정지(?:처분|결정|명령)"),
    ("crime", "기소", r"검찰기소|구속기소|(?:횡령|배임).{0,12}?기소"),
    ("crime", "수사착수", r"수사착수|압수수색|구속영장(?:청구|발부)"),
    ("liquidity", "미상환", r"미상환|미지급|상환불능|지급불능|차환실패|이자연체"),
    ("liquidity", "지급의무확정", r"(?:주식매수청구권|상환|지급).{0,25}?(?:의무확정|청구확정|대금확정|지급명령)"),
    ("liquidity", "지급부담현실화", r"(?:주식매수청구권|지급|상환).{0,30}?(?:부담현실화|자금부족|유동성위기)"),
    ("liquidity", "자금부족확대", r"(?:자금부족|유동성부족|부족자금|손실|채무).{0,25}?(?:확대|증가|급증|발생)"),
    ("credit", "등급강등", r"(?:신용)?등급.{0,12}?(?:강등|하향)"),
    ("operations", "영업중단", r"영업중단|공장가동중단|생산중단|대규모리콜|중대재해"),
]
_BACKGROUND = re.compile(r"지난|작년|당시|앞서|기존|그동안|계류|상태|바있|받은바|"
                         r"\d+년연속|문턱|여파|이후|결정했던|신청했던|받았던")
_UNCERTAIN = re.compile(r"가능성|전망|우려|예상|여부|할까|될까|검토중|검토하고|추진중")
# Resolution polarity is local to the outcome. Dismissal of a delisting stay is
# adverse; dismissal of a creditor's bankruptcy petition is not the same event.
_RESOLVED_STAGES = {
    "가처분신청": r"(?:가처분|효력정지|집행정지).{0,12}?인용",
    "거래정지": r"거래정지해제|거래재개",
    "상장폐지결정": r"상장유지|상장폐지.{0,5}?(?:철회|취소)",
    "상장폐지사유": r"상장유지|상장폐지.{0,5}?(?:철회|취소)",
    "감사의견거절": r"거절.{0,4}?적정",
    "과징금": r"(?:과징금|제재).{0,10}?(?:취소|철회)",
}
_AMOUNT = re.compile(r"\d[\d,]*(?:\.\d+)?(?:조|억|만)(?:\d[\d,]*(?:\.\d+)?(?:억|만))*(?:원)?")
_DATE = re.compile(r"(?:20\d{2}년)?\d{1,2}월\d{1,2}일|20\d{2}[-./]\d{1,2}[-./]\d{1,2}")


def _amounts(src):
    values = set()
    units = {"조": 10**12, "억": 10**8, "만": 10**4}
    for amount in _AMOUNT.findall(src):
        total = sum(Decimal(number.replace(",", "")) * units[unit]
                    for number, unit in re.findall(r"(\d[\d,]*(?:\.\d+)?)(조|억|만)", amount))
        values.add(str(int(total)))
    return sorted(values)


def _identities(src):
    identities = set()
    for label, pattern in {
        "금융위": r"금융위원회|금융위", "금감원": r"금융감독원|금감원",
        "공정위": r"공정거래위원회|공정위", "증선위": r"증권선물위원회|증선위",
        "거래소": r"한국거래소|거래소", "국세청": r"국세청",
        "전단채": r"전단채|전자단기사채", "회사채": r"회사채",
        "CP": r"기업어음|CP", "CB": r"전환사채|CB",
        "주식매수청구권": r"주식매수청구권", "대여금": r"대여금",
        "추가": r"추가(?:제재|과징금|부과|부도|손실)",
    }.items():
        if re.search(pattern, src):
            identities.add(label)
    # Explicit accounting periods, including headline shorthand "2026년 감사".
    # Do not derive a fiscal year from pubDate or an ordinary YYYY년MM월 date.
    years = re.findall(r"(20\d{2})(?:사업연도|회계연도|년(?:도|반기|[1-4]분기|결산))", src)
    years += re.findall(r"(20\d{2})년(?:도)?(?:회계연도|사업연도|감사의견|감사보고서|의견)", src)
    identities.update("회계연도:" + year for year in years)
    identities.update(re.findall(r"20\d{2}년(?:(?:상|하)반기|[1-4]분기)", src))
    # Court abbreviations retain the jurisdiction and appeal level. Avoid a
    # greedy Hangul prefix, which can accidentally include the company name.
    for place, branch, kind in re.findall(
            r"(서울|수원|대전|대구|부산|광주|울산|인천|춘천|청주|전주|창원|제주|의정부)"
            r"(중앙|동부|서부|남부|북부)?(지방법원|고등법원|회생법원|지법|고법)", src):
        identities.add(place + branch + {"지법": "지방법원", "고법": "고등법원"}.get(kind, kind))
    identities.update(re.findall(r"대법원|헌법재판소|재항고|항고|항소|상고|[1-3]심|20\d{2}[가-힣]{1,3}\d{2,}", src))
    return sorted(identities)


def event_families(title, desc="", event_type=""):
    src = _compact(title + " " + desc)
    families = {key for key, pattern in _FAMILIES.items() if re.search(pattern, src)}
    # Fallback only when source does not identify a family; model labels never
    # replace a source-derived identity.
    if not families:
        families = {key for key, pattern in _FAMILIES.items() if re.search(pattern, _compact(event_type))}
    return families


def _event_clauses(text):
    # Numeric separators belong to amounts; other commas/ellipses can separate
    # an old stage from a new action. Subject-only fragments ("금융위,") belong
    # to the next action, including any date/background qualification.
    text = re.sub(r"(?<=\d),(?=\d)", "", text or "")
    for sentence in re.split(
            r"(?<!\d)[.!?。](?!\d)|;|이후|한편|반면|그러나|하지만|(?:지만|으며|면서)[,\s]", text):
        prefix = ""
        for clause in re.split(r"[,…]", sentence):
            src = _compact(clause)
            if not src:
                continue
            if not any(re.search(pattern, src) for _, _, pattern in _STAGES):
                prefix += src
                continue
            yield prefix + src
            prefix = ""


def material_facts(title, desc=""):
    """Concrete adverse developments in headline or individual lead clauses.

    Lead evidence requires an explicit action; a bare risk keyword never opens
    the new-stage exemption. Historic/uncertain clauses are ignored separately
    so a real new decision is not lost because another sentence has background.
    """
    facts = []
    for text, headline in ((title, True), (desc, False)):
        for src in _event_clauses(text):
            if not src or _BACKGROUND.search(src) or _UNCERTAIN.search(src):
                continue
            for family, stage, pattern in _STAGES:
                match = re.search(pattern, src)
                if not match:
                    continue
                # A background modifier is not a newly imposed halt/opinion.
                if stage in {"거래정지", "감사의견거절"} and re.search(
                        r"^(?:로|속|중|상태|에따른|에빠진|받은|받았던)", src[match.end():]):
                    continue
                if stage in _RESOLVED_STAGES and re.search(_RESOLVED_STAGES[stage], src):
                    continue
                if not headline and not re.search(
                        r"결정|확정|통보|부과|의결|선고|내렸|신청했|발생|착수|발부|"
                        r"기각|각하|개시|미상환|미지급|연체|불능|실패|강등|하향", src):
                    continue
                # Dates must be attached to the event clause, never pubDate.
                amounts = _amounts(src) if family in {"sanction", "liquidity", "crime"} else []
                dates = sorted(set(_DATE.findall(src)))
                facts.append({"family": family, "stage": stage,
                              "amounts": amounts, "dates": dates, "identities": _identities(src)})
    # Keep distinct source facts, without duplicate title/lead entries.
    return [fact for i, fact in enumerate(facts) if fact not in facts[:i]]


def is_background_recap(title, desc=""):
    """Narrow known-case cap: background/prospective article, no adverse change.

    Unknown developments with active adverse wording are left to the existing
    risk review. A monetary 'hurdle' alone is not proof of a new obligation.
    """
    src = _compact(title)
    if material_facts(title, desc):
        return False
    if re.search(r"발생|확정|현실화|급증|확대|악화|불능|실패|발각|유동성위기|자금난|횡령|배임", src):
        return False
    return bool(re.search(r"거래정지속|문턱|계류|물적분할|전자투표|주총|허들|"
                          r"경과|전망|갈림길|협의중|진행중|(?:회생|상폐|상장폐지).{0,5}?(?:위기|기로)", src))


def _new_fact(fact, previous):
    same = [p for p in previous if p.get("stage") == fact["stage"]]
    if not same:
        return True
    # A headline omitting known details is a recap, not a different event.
    amounts, dates = set(fact["amounts"]), set(fact["dates"])
    identities = set(fact.get("identities", []))
    return not any(amounts <= set(p.get("amounts", [])) and
                   dates <= set(p.get("dates", [])) and
                   identities <= set(p.get("identities", [])) for p in same)


class EventHistory:
    def __init__(self, data=None, now=None):
        self.now = now or datetime.now(KST)
        self.records = []
        if not isinstance(data, dict):
            data = {}
        records = data.get("records", [])
        for record in records if isinstance(records, list) else []:
            try:
                first = datetime.fromisoformat(record["first_sent"])
                last = datetime.fromisoformat(record["last_sent"])
                if (record.get("scope") not in {"self", "full"} or not record.get("entity")
                        or not record.get("family") or first > last or last > self.now
                        or last < self.now - timedelta(days=RETENTION_DAYS)
                        or not isinstance(record.get("facts"), list)):
                    continue
                facts = [f for f in record["facts"] if isinstance(f, dict)
                         and isinstance(f.get("stage"), str)
                         and isinstance(f.get("amounts"), list) and isinstance(f.get("dates"), list)]
                self.records.append(dict(record, facts=facts[-MAX_FACTS:],
                                         titles=[t for t in record.get("titles", []) if isinstance(t, str)][-10:]))
            except (KeyError, TypeError, ValueError):
                continue
        self.records = sorted(self.records, key=lambda r: r["last_sent"])[-MAX_RECORDS:]

    def matching(self, entity, families, scope):
        return [r for r in self.records if r["entity"] == entity and r["scope"] == scope
                and r["family"] in families]

    def new_material(self, entity, title, desc="", scope="full"):
        facts = material_facts(title, desc)
        records = self.matching(entity, {f["family"] for f in facts}, scope)
        previous = [f for r in records for f in r["facts"]]
        return any(_new_fact(fact, previous) for fact in facts)

    def duplicate(self, entity, title, desc="", event_type="", scope="full"):
        # The primary headline must match. An old listing sentence appended to
        # an unrelated story is not evidence that the new story was delivered.
        families = event_families(title)
        records = self.matching(entity, families, scope)
        if not families or families - {r["family"] for r in records}:
            return False
        if self.new_material(entity, title, desc, scope):
            return False
        titles = [t for r in records for t in r.get("titles", [])]
        # Unknown new money is reviewable information, never silently a recap.
        known_amounts = {v for t in titles for v in _amounts(t)}
        if set(_amounts(_compact(title))) - known_amounts:
            return False
        # No structured event evidence: only strong textual recap evidence can
        # suppress. Fail open for unfamiliar facts outside our finite vocabulary.
        threshold = 0.6 if material_facts(title) else 0.72
        return any(SequenceMatcher(None, _compact(title), t).ratio() >= threshold for t in titles)

    def age_days(self, entity, title, desc="", event_type="", scope="full"):
        records = self.matching(entity, event_families(title, desc, event_type), scope)
        return max(((self.now - datetime.fromisoformat(r["first_sent"])).days
                    for r in records), default=0)

    def record(self, entity, title, desc="", event_type="", scope="full"):
        if not entity:
            return
        facts = material_facts(title, desc)
        for family in event_families(title, desc, event_type):
            previous = self.matching(entity, {family}, scope)
            if previous:
                record = previous[0]
            else:
                record = {"scope": scope, "entity": entity, "family": family,
                          "first_sent": self.now.isoformat(), "last_sent": self.now.isoformat(),
                          "facts": [], "titles": []}
                self.records.append(record)
            record["last_sent"] = self.now.isoformat()
            normalized_title = _compact(title)
            record.setdefault("titles", [])
            if normalized_title and normalized_title not in record["titles"]:
                record["titles"] = (record["titles"] + [normalized_title])[-10:]
            for fact in facts:
                if fact["family"] == family and fact not in record["facts"]:
                    record["facts"].append(fact)
            record["facts"] = record["facts"][-MAX_FACTS:]

    def serialize(self):
        return {"version": 1, "records": sorted(self.records, key=lambda r: r["last_sent"])[-MAX_RECORDS:]}
