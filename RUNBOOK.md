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
| 핵심 파일 | `naver_news_monitor.py`(본체), `exposure_data.csv`(익스포저), `filter_prompt*.txt`(AI 규칙) |

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
   · 429/quota → Gemini 무료 한도 초과 (Claude fallback으로 동작하나 비용 증가)
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
git pull && tail -20 run_stats.jsonl | python3 -c "
import sys,json
for l in sys.stdin:
    d=json.loads(l); t=d['gemini_ok']+d['gemini_fail']
    print(f\"{d['ts']} 수집{d['collected']:>5} 선별{d['selected']:>2} \"
          f\"Gemini {d['gemini_ok']}/{t} (fallback {d['gemini_fail']/max(t,1)*100:.0f}%) \"
          f\"{d['scope']} {d['verify_model']}\")"
```

| 필드 | 의미 |
|---|---|
| `gemini_ok` / `gemini_fail` | 1차 필터 성공/Claude fallback 횟수 |
| `gemini_model` | 그 회차에 쓴 Gemini 모델 |
| `verify_model` | 2차 검증에 쓴 Claude 모델(전체발송 시 Opus) |
| `scope` | `full`(전체발송) / `self`(본인한정) |

**fallback 비율이 높으면** Gemini 무료 티어 RPM 초과를 의심한다.
대응: 배치 크기 확대(요청 수 감소) 또는 배치 간격 확대.

## 4. GitHub Secrets 목록 (초기 셋업/재구성용)

| 시크릿 | 용도 | 없으면 |
|---|---|---|
| `EMAIL_SENDER` | 발신 Gmail 주소 | 즉시 실패 |
| `EMAIL_PASSWORD` | Gmail 앱 비밀번호 | 즉시 실패 |
| `EMAIL_RECEIVER` | 수신 그룹(쉼표 구분) | 즉시 실패 |
| `ANTHROPIC_API_KEY` | Claude(2차 검증·등급조정·action) | 즉시 실패 |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 뉴스 수집 | 즉시 실패 |
| `GOOGLE_API_KEY` | Gemini 1차 필터 | 없으면 Claude fallback(비용↑) |
| `EMAIL_CC` | 참조 수신자 | 없어도 동작 |
| `NO_RESULT_RECEIVER` | 결과없음 메일 수신자 | 없으면 발신자에게 |

**튜닝용(선택)** — 값만 넣으면 재배포 없이 조정된다. 기본값은 `VERIFY.md` 참조.

`SELF_ONLY_MAX_SCORE` · `STRONG_CAUTION_MIN_EXPOSURE` · `REF_FULLSEND_MIN_EXPOSURE` ·
`MARKET_CRASH_STOCK_THRESHOLD` · `MARKET_CRASH_RBAL_THRESHOLD`

---

## 5. 코드 수정 시

```bash
bash run_tests.sh      # 5개 테스트 + 컴파일. '전체 통과'가 아니면 커밋 금지
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
