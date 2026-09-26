# 운영 런북 — 장애 대응 / 인수인계

> 담당자 부재 시에도 이 문서만으로 시스템을 진단·복구할 수 있어야 한다.
> 상세 규칙·검증 절차는 `VERIFY.md` 참조.

---

## 1. 시스템 개요 (30초)

| 항목 | 내용 |
|---|---|
| 하는 일 | 네이버 뉴스에서 당사 익스포저 종목의 리스크 기사를 탐지해 임원진에 메일 발송 |
| 실행 주기 | 매일(주말 포함) **07 / 14 / 21시 KST** 3회 |
| 트리거 | **cron-job.org**(외부 서비스)가 GitHub Actions를 호출 — 레포에 cron 스케줄 없음 |
| 수신자 | `risk_aigent@googlegroups.com`, `risk_aigent_pb@googlegroups.com` + 발신자 본인 |
| 핵심 파일 | `naver_news_monitor.py`(본체), `exposure_data.csv`(익스포저), `filter_prompt.txt`(AI 규칙) |

---

## 2. 증상별 1차 진단

### "메일이 안 왔다"

```
1) GitHub Actions에서 해당 시각 실행 이력 확인
   https://github.com/jinhoo-choi/risk-news-crolling/actions
   → 실행 자체가 없다면? cron-job.org 문제 (아래 3-A)
   → 실행됐고 success면? 본인 한정 발송이었을 가능성 (정상 동작)
2) 로그에서 [발송판정] 줄 확인 — 왜 그렇게 판정했는지 나온다
3) Gmail 발신함 확인 — 발송은 됐는데 수신 그룹 문제일 수 있음
```

### "메일은 왔는데 내용이 이상하다"

```
1) 로그의 [2차 제외]·[dedup 해제]·[전체발송 참고 축소] 확인
2) exposure_data.csv 기준일이 최신인지 확인 (2~3일 이상 지났으면 갱신 필요)
3) 오탐이면 VERIFY.md의 '새 오탐을 만났을 때 체크리스트' 수행
```

### "실행이 실패(failure)했다"

```
1) 로그 마지막 줄의 예외 확인
2) 자주 나는 원인:
   · KeyError: 'EMAIL_SENDER' 등 → GitHub Secrets 누락/만료
   · SMTP 인증 실패 → Gmail 앱 비밀번호 만료
   · 429/quota → Anthropic 호출 한도·크레딧 확인
```

---

## 3. 복구 절차

### 3-A. cron-job.org 트리거가 멈춤 (실행 이력 자체가 없음)

레포에는 스케줄이 없으므로 **외부 트리거가 유일한 실행 수단**이다.

- 임시 조치: Actions에서 `news_monitor.yml` → **Run workflow** 수동 실행
- 항구 조치: cron-job.org 로그인 → 작업 3개(07/14/21시 KST) 상태 확인
- 계정 접근이 불가하면: `.github/workflows/news_monitor.yml`에 cron 추가
  ```yaml
  on:
    schedule:
      - cron: '0 22 * * *'   # 07시 KST (UTC-9)
      - cron: '0 5 * * *'    # 14시 KST
      - cron: '0 12 * * *'   # 21시 KST
    workflow_dispatch:
  ```
  ※ GitHub cron은 수 분~수십 분 지연될 수 있음

### 3-B. GitHub PAT 만료

증상: 데이터 갱신·수동 발송 시 `Authentication failed`

1. GitHub → Settings → Developer settings → Personal access tokens
2. 새 토큰 발급 (권한: `repo`, `workflow`)
3. 사용처: 로컬에서 `git push` 시 비밀번호 자리. **레포에 저장하지 않는다.**

### 3-C. Gmail 앱 비밀번호 만료

증상: `SMTPAuthenticationError`, 전 수신자 발송 실패

1. Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호 재발급
2. GitHub → Settings → Secrets and variables → Actions → `EMAIL_PASSWORD` 갱신

### 3-D. exposure_data.csv 갱신 중단 (장인호 대리 부재)

- 파일은 수동 업로드 → `convert_exposure.py`로 변환 → 커밋
- 갱신이 며칠 밀려도 **탐지는 계속 동작**한다(익스포저 수치만 과거 기준)
- 로그·메일 하단에 기준일이 표시되므로 임원도 인지 가능

---

## 3-E. 운영 지표 확인 (`run_stats.jsonl`)

Actions 로그는 외부망에서 내려받기 어렵고 90일 뒤 삭제되므로, 튜닝 판단에
필요한 최소 지표를 레포에 누적한다.

```bash
git pull
python3 - <<'PY_STATS'
import json
from pathlib import Path
for line in Path("run_stats.jsonl").read_text().splitlines()[-20:]:
    row = json.loads(line)
    print(row["ts"], "수집", row["collected"], "LLM투입", row.get("filter_input_count", "과거기록 없음"),
          "선별", row["selected"], row.get("filter_model", "과거 Gemini/Claude 혼합"),
          row["scope"], "테스트", row.get("is_test", "과거기록 미구분"))
PY_STATS
```

| 필드 | 의미 |
|---|---|
| `filter_model` / `verify_model` | 1차/2차 모델 설정. 미호출 단계의 비용은 `llm`으로 확인 |
| `llm` | 모델·단계별 호출수, 일반 입력/출력/캐시 읽기/쓰기 토큰 |
| `filter_input_count` | 하드룰 이후 실제 LLM 투입량. 중복률 분모 |
| `scope` / `is_test` | 최종 발송 범위 / 강제 본인한정 테스트 여부 |
| `run_id` / `commit` | Actions 실행과 코드 버전 추적 |

필터 생략 최적화와 실제 API 비교 도구는 PR #1의 검증 브랜치에 있으며 품질 기준 미달로 병합 보류 중이다.
비교 결과·원본 JSON과 운영 반영 범위는 `docs/REVIEW_2026-09-26.md`를 확인한다.
비용은 정규 회차와 테스트를 구분하고 일반 입력·캐시 토큰의 중복 합산 없이 계산한다.
과거 Gemini 필드가 있는 행은 이전 스키마이며 삭제하거나 0원으로 간주하지 않는다.

## 4. GitHub Secrets 목록 (초기 셋업/재구성용)

| 시크릿 | 용도 | 없으면 |
|---|---|---|
| `EMAIL_SENDER` | 발신 Gmail 주소 | 즉시 실패 |
| `EMAIL_PASSWORD` | Gmail 앱 비밀번호 | 즉시 실패 |
| `EMAIL_RECEIVER` | 수신 그룹(쉼표 구분) | 즉시 실패 |
| `ANTHROPIC_API_KEY` | Claude 전 단계 | 즉시 실패 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 뉴스 수집 | 즉시 실패 |
| `GOOGLE_API_KEY` | 과거 Gemini 복원용으로만 보관 | 현재 실행에 영향 없음 |
| `EMAIL_CC` | 참조 수신자 | 없어도 동작 |
| `NO_RESULT_RECEIVER` | 결과없음 메일 수신자 | 없으면 발신자에게 |

**튜닝용 환경변수(선택)** — Secrets 등록만으로는 적용되지 않는다. 해당 워크플로의 `env`에 명시적으로 연결해야 한다. 기본값은 `naver_news_monitor.py`를 기준으로 확인한다.

`SELF_ONLY_MAX_SCORE` · `STRONG_CAUTION_MIN_EXPOSURE` · `REF_FULLSEND_MIN_EXPOSURE` ·
`MARKET_CRASH_STOCK_THRESHOLD` · `MARKET_CRASH_RBAL_THRESHOLD`

---

## 5. 코드 수정 시

```bash
bash run_tests.sh      # 7개 테스트 + 컴파일. '전체 통과'가 아니면 커밋 금지
```

상세 절차·오탐 대응은 `VERIFY.md`.

---

## 6. 알려진 단일 장애점

| 항목 | 위험 | 완화책 |
|---|---|---|
| cron-job.org | 계정 만료 시 전면 중단 | 3-A의 GitHub cron 백업 |
| Gmail 앱 비밀번호 | 만료 시 전 발송 중단 | 만료 알림 설정 권장 |
| 담당자 1인 | 코드·운영·판단 단독 | **백업 담당자 지정 필요** |
| CSV 수동 업로드 | 업로더 부재 시 갱신 중단 | 며칠 지연은 허용 가능 |

---

## 7. 확정된 설계 판단 (재논의 불필요)

검수 때마다 다시 제기되지 않도록, 확인을 거쳐 확정한 사항을 남긴다.

### 7-1. 담보유지비율은 '고객 단위 최저' (2026-09-01 확정)

집계 쿼리의 담보비율은 `group by csno` 기준이라, 표에 종목별로 표시되더라도
**그 고객이 보유한 전 계좌 중 최저값**이다. 해당 종목 계좌의 비율이 아니다.

- 서브쿼리에 `PDNO` 조인이 없는 것은 **의도된 설계**다. 조인 추가 검토 불필요.
- 사유: 한 고객의 계좌 여러 개 중 가장 취약한 계좌를 기준으로 보여주는 편이
  리스크 모니터링에 안전하고, 담당자가 연락할 때 필요한 정보와도 맞다.

### 7-2. 위험 구간(140~150%) 필터는 min()에도 적용 (2026-09-01 수정 완료)

`위험계좌` 카운트에만 구간 필터가 있고 `min()`에는 없어, 위험 구간 계좌를 가진
고객의 **구간 밖 계좌 값**이 최솟값으로 잡히던 버그가 있었다.
(2026-09-01 이수페타시스 여신 138.6% — 표 헤더 문구 `140%~150%`와 불일치)

```sql
-- 수정 전
min(mgge_mntn_rt) as 뱅유지담보비율
-- 수정 후 (뱅 2곳 · 영 2곳, 총 4개 서브쿼리 모두)
min(case when mgge_mntn_rt >= 1.4 and mgge_mntn_rt < 1.5
         then mgge_mntn_rt end) as 뱅유지담보비율
```

`min()`이 NULL을 무시하므로 위험 구간 계좌 중 최솟값만 남는다. 각 블록에
`위험계좌 > 0` 조건이 있어 NULL은 발생하지 않는다.

**신규 CSV 수령 시 점검 명령** — 두 값 모두 0이어야 한다.

```bash
awk -F',' 'NR>1 && $12!="" && $12+0<140' exposure_data.csv | wc -l   # 뱅
awk -F',' 'NR>1 && $20!="" && $20+0<140' exposure_data.csv | wc -l   # 영
```
