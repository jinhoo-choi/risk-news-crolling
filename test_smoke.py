"""main() 전 구간 스모크 테스트 — 외부 호출만 모의하고 실제 코드를 그대로 실행.

배경: 2026-07-29 실환경 테스트가 NameError(ref_date 정의 전 사용)로 중단됐다.
단위 테스트들은 decide_send_scope 등을 '직접' 호출하므로 main()의 변수 정의
순서를 타지 않아 이 오류를 잡지 못했다.

이 테스트는 네트워크·SMTP·AI만 모의하고 main()을 끝까지 실행해,
변수 스코프·호출 순서·타입 오류처럼 '연결해야만 드러나는' 결함을 잡는다.
실패 시 종료코드 1.
"""
import sys, os, types, json, io, contextlib
import pandas as pd

# ── 외부 의존성 모의 ────────────────────────────────────────────────────
_SENT = []


class _FakeTicker:
    def __init__(self, tk):
        self.tk = tk

    def history(self, **k):
        from datetime import datetime, timezone, timedelta
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).strftime("%Y-%m-%d")
        idx = pd.to_datetime([f"{today} 09:00", f"{today} 15:30"]).tz_localize("Asia/Seoul")
        return pd.DataFrame({"Close": [10000, 9300]}, index=idx)   # -7%


_yf = types.ModuleType("yfinance")
_yf.Ticker = _FakeTicker
sys.modules["yfinance"] = _yf

for _k, _v in [("EMAIL_SENDER", "me@test.com"), ("EMAIL_PASSWORD", "x"),
               ("EMAIL_RECEIVER", "grp@test.com"), ("ANTHROPIC_API_KEY", "x"),
               ("NAVER_CLIENT_ID", "x"), ("NAVER_CLIENT_SECRET", "x"),
               ("GOOGLE_API_KEY", "x")]:
    os.environ.setdefault(_k, _v)
os.environ["FORCE_SELF_ONLY"] = "1"      # 혹시라도 실제 발송 경로를 타지 않도록

import requests
import smtplib


class _Resp:
    """requests.Response 최소 호환 객체 — 실제 코드가 쓰는 속성/메서드를 모두 갖춘다."""

    def __init__(self, payload, code=200):
        self._p = payload
        self.status_code = code
        self.ok = 200 <= code < 300
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode()
        self.encoding = "utf-8"
        self.headers = {"Content-Length": str(len(self.content)),
                        "Content-Type": "application/json"}

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self):
        pass


# 실제 필터를 통과하도록 구성한 모의 기사.
# 하드제외·dedup·AI 필터를 모두 지나 발송판정까지 도달해야 테스트가 의미 있다.
def _recent(minutes_ago: int) -> str:
    """수집 윈도우(14시간) 안에 들어오는 pubDate 생성.

    ★고정 날짜를 쓰면 시간이 지나면서 윈도우를 벗어나 뉴스가 0건이 되고,
      테스트가 조용히 무의미해진다(2026-07-29 실측 — 전 키워드 0건).
      항상 '지금 기준'으로 만든다.
    """
    from datetime import datetime, timezone, timedelta
    kst = timezone(timedelta(hours=9))
    t = datetime.now(kst) - timedelta(minutes=minutes_ago)
    return t.strftime("%a, %d %b %Y %H:%M:%S +0900")


_NEWS = {
    "items": [
        {"title": "A사 <b>부도</b> 발생…법정관리 신청",
         "originallink": "http://news.test/1", "link": "http://news.test/1",
         "description": "A사가 부도 처리됐다.",
         "pubDate": _recent(30)},
        {"title": "삼성전자 급락…코스피 흔들",
         "originallink": "http://news.test/2", "link": "http://news.test/2",
         "description": "반도체주 약세.",
         "pubDate": _recent(60)},
    ]
}


def _fake_get(url, *a, **k):
    if "openapi.naver.com" in url:
        return _Resp(_NEWS)
    return _Resp({}, 200)


def _fake_post(url, *a, **k):
    """Gemini·Claude 응답 모의 — 요청 형태로 어떤 호출인지 구분."""
    body = k.get("json", {}) or {}
    # Gemini
    if "generativelanguage" in url:
        txt = json.dumps([
            {"id": 1, "relevant": True, "grade": "긴급", "reason": "부도",
             "confidence": 0.95, "action": "확인", "entity": "A사",
             "entities": ["A사"], "event_type": "부도", "related_stocks": []},
            {"id": 2, "relevant": True, "grade": "참고", "reason": "급락",
             "confidence": 0.6, "action": None, "entity": "삼성전자",
             "entities": ["삼성전자"], "event_type": "주가급락", "related_stocks": []},
        ], ensure_ascii=False)
        return _Resp({"candidates": [{"content": {"parts": [{"text": txt}]}}]})
    # Claude — 호출 종류를 프롬프트의 '요구 응답 형식'으로 구분한다.
    # (프롬프트 본문 어휘는 서로 겹쳐서 오분기가 난다 — 실측 확인)
    prompt = json.dumps(body, ensure_ascii=False)
    if "relevant" in prompt:                    # 1차 필터(Claude fallback)
        txt = json.dumps([
            {"id": 1, "relevant": True, "grade": "긴급", "reason": "부도 확정",
             "confidence": 0.95, "action": "확인", "entity": "A사",
             "entities": ["A사"], "event_type": "부도", "related_stocks": []},
            {"id": 2, "relevant": True, "grade": "참고", "reason": "급락",
             "confidence": 0.6, "action": None, "entity": "삼성전자",
             "entities": ["삼성전자"], "event_type": "주가급락", "related_stocks": []},
        ], ensure_ascii=False)
    elif "risk" in prompt and "judgment" in prompt:   # 2차 본문검증
        txt = json.dumps({"risk": True, "reason": "유지",
                          "judgment": {"핵심사건": "부도", "손실주체": "A사",
                                       "당사연관": "직접", "확정여부": "확정"}},
                         ensure_ascii=False)
    elif "현재등급" in prompt:                     # 등급 재검증
        txt = json.dumps([{"id": i, "grade": "긴급"} for i in range(1, 20)],
                         ensure_ascii=False)
    elif "action" in prompt or "대응방안" in prompt:   # action 생성
        txt = json.dumps({"action": "보유 고객 담보비율 점검",
                          "customer_notice": None}, ensure_ascii=False)
    else:                                        # 기타(action 등)
        txt = json.dumps({"action": "보유 고객 담보비율 점검",
                          "customer_notice": None}, ensure_ascii=False)
    # 실제 Anthropic 응답은 블록에 type이 있고, 코드가 type=="text"만 취한다
    return _Resp({"content": [{"type": "text", "text": txt}],
                  "stop_reason": "end_turn"})


class _FakeSMTP:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, frm, to, msg):
        _SENT.append(to[0])
        return {}


requests.get = _fake_get
requests.post = _fake_post
smtplib.SMTP_SSL = _FakeSMTP


def main():
    # ★운영 지표 파일 오염 방지(2026-07-29 발견):
    #   스모크 테스트는 더미 API 키로 돌기 때문에 Gemini가 항상 실패한다.
    #   그 결과가 run_stats.jsonl에 섞이면 '실제 fallback률'을 왜곡한다.
    #   테스트 중에는 임시 경로로 기록하도록 우회한다.
    import tempfile, shutil
    global _BACKUP_DIR
    _tmp = tempfile.mkdtemp()
    _BACKUP_DIR = _tmp
    _orig_save = None

    # dedup 상태가 모의 기사를 걸러내지 않도록 임시 파일로 격리.
    # ★백업만 하고 복원하지 않으면 다음 실행이 빈 seen_news로 시작해
    #   테스트 결과가 달라진다(2026-07-29 실측 — 스모크가 dedup에 막혀
    #   뉴스 0건이 되면서 도달 검증이 실패했다). _restore에서 반드시 되돌린다.
    for _f in ["seen_news.json"]:
        if os.path.exists(_f):
            shutil.copy(_f, os.path.join(_tmp, _f))
    with open("seen_news.json", "w", encoding="utf-8") as f:
        f.write("{}")

    print("=" * 74)
    print("[스모크 테스트] main() 전 구간 실행")
    print("=" * 74)
    import importlib.util
    spec = importlib.util.spec_from_file_location("nm", "naver_news_monitor.py")
    nm = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(nm)
    except SystemExit:
        pass

    # 지표 기록을 임시 경로로 우회 — 운영 파일(run_stats.jsonl) 보호
    _orig_save = nm.save_run_stats
    _tmp_stats = os.path.join(_tmp, "run_stats.jsonl")
    nm.save_run_stats = lambda *a, **k: _orig_save(*a, **{**k, "path": _tmp_stats})

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            nm.main()
    except Exception as e:
        out = buf.getvalue()
        print(f"  FAIL {type(e).__name__}: {e}")
        print("\n  [실행 로그 마지막 30줄]")
        for l in out.splitlines()[-30:]:
            print(f"    {l}")
        import traceback
        print("\n  [트레이스백]")
        traceback.print_exc()
        return 1

    out = buf.getvalue()
    print("  OK   main() 예외 없이 완주")
    print(f"  발송 수신자: {_SENT if _SENT else '(없음)'}")
    leaked = [r for r in _SENT if r != "me@test.com"]
    if leaked:
        print(f"  FAIL FORCE_SELF_ONLY인데 외부 발송: {leaked}")
        return 1
    print("  OK   FORCE_SELF_ONLY — 외부 발송 없음")

    # ★핵심: 발송판정 지점까지 실제로 도달했는지 확인한다.
    #   2026-07-29 실환경 실패(ref_date UnboundLocalError)가 바로 이 지점에서
    #   났는데, 초기 스모크 테스트는 그 앞에서 종료돼 잡지 못했다.
    #   '예외 없이 끝났다'가 아니라 '해당 코드를 지났다'를 검증해야 한다.
    reached = True
    for key in ["[2차 검증 모델", "[발송판정]"]:
        ok = key in out
        if not ok:
            reached = False
        print(f"  {'OK  ' if ok else 'FAIL'} 로그 '{key}' 도달")
    if not reached:
        print("\n  FAIL 발송판정 경로에 도달하지 못함 — 테스트가 무의미하다.")
        print("       모의 기사가 필터에서 전부 걸러졌을 가능성. 아래 로그 확인:")
        for l in out.splitlines()[-25:]:
            print(f"    {l}")
        return 1
    print("\n  [실행 로그 요약]")
    for l in out.splitlines():
        if any(t in l for t in ["[발송판정]", "[2차 검증 모델", "[운영지표]",
                                "[시장급락", "본인 한정", "전체 발송"]):
            print(f"    {l.strip()}")
    return 0


_BACKUP_DIR = None


def _restore():
    """테스트가 건드린 상태 파일 원복 — 정규 회차 dedup 오염 방지."""
    import glob
    import shutil as _sh
    # ① seen_news.json 원복 (백업이 있으면)
    if _BACKUP_DIR:
        _b = os.path.join(_BACKUP_DIR, "seen_news.json")
        if os.path.exists(_b):
            _sh.copy(_b, "seen_news.json")
    # ② 테스트가 만든 로그 정리
    for f in glob.glob("filter_log_*.json"):
        try:
            os.remove(f)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        _rc = main()
    finally:
        _restore()
    sys.exit(_rc)
