"""커밋 전 자기 점검 — '내 변경이 의도대로 들어갔는가'를 검사한다.

배경(2026-07-29): 하루에 결함 9건이 나왔고, 그중 2건은 코드 품질이 아니라
'수정이 실제로 파일에 반영되지 않았는데 반영됐다고 믿은 것'이 원인이었다.
  · 강등 잠금: 설정 코드만 들어가고 확인 코드가 통째로 누락됐는데
    커밋됨 → 이후 4곳으로 확대했지만 읽는 쪽이 없어 전부 무효
  · 테스트는 '플래그가 설정되는가'만 봐서 통과

이 스크립트가 잡는 것:
  ① 정의했지만 아무도 읽지 않는 식별자(고아 플래그/상수/함수)
  ② 변경 규모가 의도와 맞는지(파일별 추가/삭제 라인)
  ③ 검증 스위트 미실행 상태로 커밋 시도

사용: python3 check_changes.py
"""
import ast
import re
import subprocess
import sys

TARGET = "naver_news_monitor.py"


def _sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def orphan_identifiers(path=TARGET):
    """'쓰기만 하고 읽지 않는' 식별자 탐지.

    플래그·상태를 도입할 때 생산자만 만들고 소비자를 빠뜨리는 실수를 잡는다.
    - a["_flag"] = ... 는 있는데 a.get("_flag") / a["_flag"] 읽기가 없는 경우
    - 모듈 전역 상수를 정의만 하고 참조하지 않는 경우
    """
    src = open(path, encoding="utf-8").read()
    problems = []

    # ① dict 키 플래그 (a["_xxx"] = 형태)
    written = set(re.findall(r'\w+\[\s*"(_[a-z_]+)"\s*\]\s*=', src))
    for key in sorted(written):
        # 읽기: .get("key") 또는 ["key"] 가 대입 좌변이 아닌 위치에 등장
        reads = len(re.findall(rf'\.get\(\s*"{re.escape(key)}"', src))
        reads += len(re.findall(rf'\[\s*"{re.escape(key)}"\s*\](?!\s*=)', src))
        if reads == 0:
            problems.append(("고아 플래그", key,
                             "설정만 하고 읽는 코드가 없음 — 소비자 누락 의심"))

    # ② 모듈 전역 상수(대문자) 정의 후 미참조
    tree = ast.parse(src)
    consts = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 3:
                    consts[t.id] = n.lineno
        elif isinstance(n, ast.Try):
            for sub in ast.walk(n):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 3:
                            consts[t.id] = sub.lineno
    for name, ln in consts.items():
        # 단어 경계 기준 전체 출현 수 — 정의 1회를 빼고도 남으면 사용 중.
        # (기존 '뒤에 = 이 없는 것만' 방식은 KEYWORDS + overseas 같은 정상
        #  사용을 놓쳐 오탐이 대량 발생했다)
        uses = len(re.findall(rf'(?<![\w.]){re.escape(name)}(?![\w])', src))
        if uses <= 1:
            problems.append(("미사용 상수", f"{name} (L{ln})",
                             "정의만 하고 참조하지 않음"))

    # ③ 정의만 하고 호출되지 않는 함수(테스트 헬퍼 제외)
    _ENTRY = {"main"}          # 진입점은 __main__에서만 호출되므로 제외
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name not in _ENTRY:
            calls = len(re.findall(rf'(?<![\w.]){re.escape(n.name)}\s*\(', src))
            if calls <= 1:
                problems.append(("미호출 함수", f"{n.name} (L{n.lineno})",
                                 "정의만 하고 호출하지 않음"))
    return problems


def diff_summary():
    stat = _sh("git diff --stat").strip()
    staged = _sh("git diff --cached --stat").strip()
    return stat, staged


def main():
    print("=" * 78)
    print("[커밋 전 자기 점검]")
    print("=" * 78)
    rc = 0

    print("\n① 변경 규모")
    stat, staged = diff_summary()
    if not stat and not staged:
        print("   (변경 없음)")
    for label, s in [("미스테이지", stat), ("스테이지됨", staged)]:
        if s:
            print(f"   [{label}]")
            for line in s.splitlines():
                print(f"     {line}")

    print("\n② 고아 식별자 — 정의했지만 읽지 않는 것")
    probs = orphan_identifiers()
    if not probs:
        print("   OK   없음")
    else:
        for kind, name, why in probs:
            # 미호출 함수는 진입점·핸들러가 많아 경고만
            level = "FAIL" if kind == "고아 플래그" else "경고"
            if level == "FAIL":
                rc = 1
            print(f"   {level} [{kind}] {name}")
            print(f"          {why}")

    print("\n③ 검증 스위트")
    r = subprocess.run("bash run_tests.sh", shell=True,
                       capture_output=True, text=True)
    tail = [l for l in r.stdout.splitlines() if "전체 통과" in l or "실패 있음" in l]
    print(f"   {tail[0].strip() if tail else '(실행 결과 확인 불가)'}")
    if r.returncode != 0:
        rc = 1

    print("\n" + "=" * 78)
    print(" ✅ 커밋 가능" if rc == 0 else " ❌ 위 항목 확인 후 커밋")
    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
