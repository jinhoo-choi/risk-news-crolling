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

## 경쟁사 공지 수집 상태 (2026-10-07)

- 공지 수집은 `broker_notices.py`. KB·신한·키움 URL 및 공통 파서, CSV `date,company,title,url`, `(company,title)` 중복 제거, 30일 보관은 유지한다. `date`는 기존처럼 수집일이다.
- **1개 이상 소스의 HTTP/연결/응답 구조/CSV 저장 오류 → `degraded`, exit 1.** 모든 소스를 시도한 뒤 판정한다. 정상 수집분은 저장한다.
- `broker_notices_status.json`: KST 확인시각, 전체 상태, 실패 소스 수, 소스별 HTTP/최종 URL/상태/원본 파싱 건수(`count`)/필터 일치 건수(`retained_count`)/오류. Actions Summary에도 표로 기록한다. `broker-notices-status-{run_id}` artifact로 7일 보관한다. JSON은 CSV 수집 폴더 밖에 둔다.
- `ok`: 필터 전 1건 이상 파싱(키워드 일치 0건이어도 정상). `empty`: HTTP 성공·필터 전 0건. KB·신한·키움의 기존 공통 파서는 목록 완전성을 검증하지 않으며 `legacy_generic`으로 구분한다. 기존 키움 0건이 실제 무공지라는 의미는 아니다.
- 공지 작업의 `continue-on-error`는 제거했다. 실패하더라도 artifact 업로드와 정상 CSV 커밋 단계는 `always()`로 수행한다. 본 뉴스 작업은 기존 `needs` 및 `if: always()`를 유지하므로 공지 오류 때문에 중단되지 않는다.

### 공개 응답 확인

2026-10-07 06:21 KST 전후 로컬 읽기 전용 GET에서 수정 코드로 확인. 운영 워크플로·AI 필터·메일·CSV 저장은 실행하지 않았다. 첫 페이지 범위이며 과거 전수 수집은 아니다.

| 소스 | 공개 목록 | 실제 구조 | HTTP / 파싱 건수 |
|---|---|---|---|
| 미래에셋 | `https://securities.miraeasset.com/bbs/board/message/list.do?categoryId=66&listType=1&curPage=1` | `table.bbs_linetype2 .subject a`, `javascript:view(messageId, …)` → `view.do` | 200 / 10 |
| 삼성 | `https://www.samsungpop.com/mbw/customer/noticeEvent.do?cmd=noticeList`의 공개 `getNoticeList` 요청 | `totalCount`, `fixlist` + `list`, `ntcTitle1`, `menuSeqNo` → `noticeView` | 200 / 10 |
| NH | `https://www.nhsec.com/wooriwmBoard/boardList.action?sBoard_Id=1&sType_Cd=0000000002` | `table.tblType`, `onclick=viewUp(…)` → `boardView.action` | 200 / 12 (고정 2 + 일반 10) |
| 토스 | `https://corp.tossinvest.com/ko/notice`의 공개 `https://docs-api.tossinvest.com/api/v1/post/search?type=NOTICE&page=0&size=20` | `result.list`, `pagingParam`, `id`, `category.id`; `NOTICE`/`notice` 혼용 → `/ko/post` | 200 / 20 |

삼성 요청의 `ntcSect=3`, `siteGubun=P`, `sortColumn=ProcDTime2` 등은 공개 목록 페이지의 요청 형식이다. 토스 경로는 공지 페이지 JS의 `DOCS` base 및 `/api/v1/post/search`에서 확인했다. 미래에셋·NH는 EUC-KR HTML, 삼성·토스는 JSON이다. HTML에 메뉴만 있거나 JSON 오류/총건수와 빈 목록이 불일치하면 수집 실패로 기록한다.

`tests/fixtures/broker_notices/`는 위 실제 응답에서 목록 테이블/파서 필드만 추출한 UTF-8 fixture다. 본문·메뉴·개인정보는 넣지 않았다. `python test_broker_notices.py`는 오프라인 회귀(4곳 파서, 링크, 인코딩, 고정글, 타입 혼용, 404/timeout/구조 변경, 4곳 실패 degraded, 부분 성공 보존, 정상 0건, 기존 경로/CSV 중복 제거)를 검증한다. `bash run_tests.sh` 및 `python check_changes.py`에도 포함한다.

### 공지 키워드 필터 (2026-10-08)

- 7개 소스 모두 **목록의 제목에 `신용`, `대출`, `오류` 중 하나라도 포함**된 공지만 CSV에 저장한다. 상세 본문, URL, 회사명으로는 판정하지 않으며 상세 페이지를 추가 수집하지 않는다.
- `notice_filter.matches_notice_title`을 수집 결과·CSV 저장·메일용 CSV 로더에서 공유한다. HTML 목록 파서가 먼저 태그를 제거하며, 공통 필터는 HTML entity를 디코딩하고 기존 출력 필터처럼 공백을 무시하는 부분문자열 OR 비교를 한다. `신용융자`, `담보대출`, `주문오류`는 포함하지만, `담보`, `장애`, `증거금률`만 있는 제목은 포함하지 않는다. JSON 파서의 누락/null 제목 검증은 유지한다.
- 기존 CSV의 무관한 행은 다음 정상 자연 수집 때 같은 필터로 정리한다. 새 일치 공지가 없어도 정리하며, 일치하는 기존 행은 기존 30일 보관 범위에서 유지한다. 수집 실패 소스의 CSV는 건드리지 않으며 기존 CSV 읽기 실패도 덮어쓰지 않고 오류로 기록한다. 이 변경 PR에서 데이터 CSV를 직접 수정하지 않는다.
- 메일 로더도 같은 필터로 기존 CSV를 읽으므로 과거의 넓은 키워드(`증거금률`, `한도 축소` 등)만 포함한 공지는 출력하지 않는다. 최근 2일 범위와 `(company,title)` 중복 제거는 유지한다.
- 로그와 Actions Summary는 원본 파싱 건수/키워드 일치 건수를 분리한다. `count`는 이전과 같은 필터 전 건수, `retained_count`는 이번 수집 중 필터 일치 건수이며 CSV 전체 행 수나 신규 추가 건수가 아니다. 키워드 일치 0건은 crawler 실패가 아니다.
- 고정 공개 fixture의 원본/일치 건수: 미래에셋 10/3, 삼성 10/1, NH 12/1, 토스 20/1. 현재 운영 수집량을 뜻하지 않는다. 오프라인 회귀에서 7개 소스 OR 필터, 본문만 일치하는 공지 제외, 공백/entity/HTML 제목 처리, null 제목, 혼합 목록, 건강한 0건, 기존 CSV 정리·실패 시 보존, 메일 로더 동일 필터를 검사한다.
