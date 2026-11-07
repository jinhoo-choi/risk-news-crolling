# 구독 분석 소유권과 완료 증거 계약

`review_evidence_version=1`인 새 production 회차에만 적용한다. 수집 당시 고정한 input/policy/body_manifest 해시와 구독 v2 전수 1차 처리 계약을 유지한다. 구형 결과·영수증에 필드를 추가하거나 재분석하지 않는다. 점수·등급·수신자·TTL·SMTP·예약·유료 API 설정은 이 변경의 대상이 아니다.

## 한 번의 분석 실행

1. 최신 main을 고정해 `--instructions`를 읽고, 비공개 큐의 고정 commit에서 job·outbox·result·attempt와 필요한 입력 shards를 조회한다. 만료/전송/다른 running owner면 시작하지 않는다.
2. `begin`으로 owner를 잡아 게시·재조회한다. 동일 owner 재개는 저장된 체크포인트만 재사용한다. 종료가 확인된 `blocked` owner 인계는 기존 attempt를 audit에 보존한다.
3. `risk_review_context.py`의 `--analysis` 색인에서 확정 제외 규칙을 적용하고 후보 본문을 선택한다. `--next-batch 25`로 남은 본문을 끝까지 이어간다. 제목·설명뿐 아니라 보존 본문의 위험 신호로도 읽는 순서를 앞당긴다. 이 우선순위는 판정/점수/대표를 바꾸지 않는다.
4. 실제 판단한 ID마다 기존 분류/분석과 아래 짧은 증거를 만든다. 묶음 경계에서 `checkpoint`로 저장한다. 같은 내용의 재시도는 기존 blob을 확인하며 새 파일/재분석을 만들지 않는다. 완료 ID와 다른 내용의 겹침은 차단한다.
5. `finalize`가 미시도 0과 형식·해시를 검증한다. 실제 미확보 본문은 상태 기반 checkpoint로 한 번만 추가한다. result와 ready owner를 한 transaction으로 게시·재조회한다. 이후 기존 consume 요청을 같은 실행에서 수행한다.
6. 진행 불가하면 현재 owner가 `stop --reason ...`으로 실제 종료를 기록한다. 다음 실행은 완료 checkpoint를 이어간다. heartbeat가 오래됐다는 이유만으로 다른 실행을 종료 처리하지 않는다. 외부 강제 종료 여부를 확인할 수 없으면 원인 미확인으로 남긴다.

## false/null의 짧은 판정 증거

`source_hash`는 UTF-8 원문 문자열의 SHA256이다. 공백·줄바꿈을 바꾸지 않는다. `true`는 기존 분석에 원문 해시가 맞는 본문 인용 8자 이상이 최소 1개 있으면 그 증거를 재사용한다. 후보 분석 생성 실패는 짧은 body decision_basis로 실제 시도를 남겨 해당 기사만 보류할 수 있다. 빈 true/분석만으로는 완료를 주장할 수 없다.

`checkpoint` 입력에서는 source_hash를 생략할 수 있다. 실제 판정의 kind/quote/blocker를 적으면 helper가 고정 원문에서 해시를 계산한다. metadata_rule의 rule도 기존 코드의 실제 반환값으로 채울 수 있다. 이미 적힌 해시·규칙이 틀리면 덮어 고치지 않고 차단한다. 모델이 긴 해시를 복사하거나 다시 계산할 필요가 없다. 저장된 checkpoint/result에는 완전한 증거가 남는다.

| 종류 | 필드 | 허용 조건 |
|---|---|---|
| 본문 판정 | `kind: body`, `source_hash`, `quote` | 원문 8~240자 인용. false는 실제 정상 제외. null은 아래 blocker 필수 |
| 확정 제목·설명 제외 | `kind: metadata_rule`, `source_hash`, `rule` | 기존 `naver_news_monitor.is_hard_excluded(title, description, url)`가 true와 정확히 같은 rule을 반환해야 함. 해시는 `title + "\n" + description` |
| 본문 미확보 | `kind: unavailable`, `source_hash` | helper가 본문 상태/품질로 생성. null만 허용. verified 본문을 미확보로 바꿀 수 없음 |

null의 `blocker`는 `entity`, `event_stage`, `date`, `conflicting_evidence`, `unrelated_body`, `analysis_failed` 중 실제 실패 항목이다. 관련 근거가 부족하면 null이며 false로 제외하지 않는다. 시간 부족·미열람은 미시도로 남긴다. 인용은 의미 판단의 진위를 자동 증명하지 않으므로 기존 정책에 따른 최초 원문 판단은 필요하다. 장문 감사·2차 AI 판정은 추가하지 않는다.

체크포인트는 `{job_id,input_hash,policy_hash,body_manifest_hash,covered_ids,classifications,analyses,event_groups}`이다. 최종 최고점 대표는 전체 입력의 기존 점수 산출 뒤 선정하며 서로 다른 입력 ID를 사건 중복이라는 이유로 미리 제외하지 않는다.

기사별 체크포인트는 `event_groups=[]`로 저장한다. 전체 1차 처리 뒤 사건 묶음을 한 번 비교하고, 마지막 기사 체크포인트 또는 별도의 묶음 체크포인트에 저장한다. 별도 묶음은 `covered_ids=[]`, `classifications=[]`, `analyses={}`와 전체 `event_groups`를 담는다. 저장 전 전체 계약의 형식·그룹 ID·중복을 검증하므로 오류가 있으면 원격 상태는 바뀌지 않는다. 로컬 오류만 고쳐 같은 저장을 다시 계획하며 완료 기사 판단을 재생성하지 않는다. 체크포인트 검증은 미완료 진행을 저장하기 위한 것이며 미시도 소비 차단을 완화하지 않는다.

## 연결 도구를 사용하는 오프라인 transaction 계획

CLI는 GitHub·SMTP·AI를 호출하지 않는다. 이미 내려받은 비공개 로컬 snapshot을 받아 게시할 내용을 만든다. 공개 저장소에 snapshot/transaction을 저장하지 않는다.

```json
{
  "queue_head": "비공개 저장소의 고정 commit SHA 40자리",
  "files": {
    "jobs/회차ID.json": {"sha": "실제 blob SHA", "value": {}},
    "analysis-attempts/회차ID.json": {"sha": null, "value": null},
    "results/회차ID.json": {"sha": null, "value": null},
    "outbox/회차ID.json": {"sha": null, "value": null}
  }
}
```

위 job의 `{}`는 설명용 자리이며 실제 원장을 사용한다. shards·attempt가 참조한 checkpoint·인계 archive도 동일 commit에서 조회해 넣는다. 존재하는 파일의 실제 SHA를 쓰고, 부재가 확인된 경로만 null로 쓴다. 분석 출력용 `{job,checkpoints}` snapshot과 session용 `{queue_head,files}` snapshot을 혼동하지 않는다.

새 head의 tree에서 blob SHA가 변하지 않은 입력 shards·완료 checkpoint는 최초 검증한 비공개 로컬 원본을 재사용한다. 매 묶음마다 본문 전체를 다시 다운로드하지 않는다. 변경된 attempt/job 및 새 checkpoint와 부재 조건만 갱신하고, 게시 직전 모든 expected SHA를 대조한다. 로컬 진행 검증은 저장된 판정을 합칠 뿐 AI 재분석을 요구하지 않는다.

```bash
python risk_review_session.py private-session.json begin --job-id JOB --attempt-id OWNER --source-main MAIN_SHA --automation-id AUTOMATION --output private-begin-001.json
python risk_review_context.py private-analysis.json --analysis --next-batch 25 --output private-bodies-001.json
python risk_review_session.py private-session-fresh.json checkpoint --job-id JOB --attempt-id OWNER --checkpoint private-checkpoint.json --output private-save-001.json
python risk_review_session.py private-session-fresh.json finalize --job-id JOB --attempt-id OWNER --model unconfirmed --output private-finalize-001.json
```

각 출력의 `transactions`를 연결된 GitHub 도구로 다음과 같이 게시한다.

1. private 저장소 `main`이 출력의 `queue_head`인지 확인한다. 그 commit의 tree에서 모든 `expected` 경로의 blob SHA/부재를 대조한다. 경로에는 `subscription/risk/`를 앞에 붙인다.
2. `values`만 JSON blob/tree로 만들고 해당 head를 부모로 commit을 생성한다. 관계없는 기존 tree는 base_tree로 보존한다. 삭제는 허용하지 않는다.
3. `update_ref(expected_sha=queue_head, force=false)`로 게시한다. 다른 head/lease면 멈추고 최신 snapshot으로 다시 계획한다. 시간 초과/응답 유실이면 실제 head와 파일부터 조회한다. 무조건 재쓰기하지 않는다.
4. 새 고정 head에서 쓴 파일을 재조회해 값·해시를 대조한 뒤 저장 완료로 센다. 그 snapshot으로 다음 단계를 진행한다. transactions가 비었으면 이미 저장된 동일 owner/결과의 재사용이다.

기존 인증된 Python 실행 환경은 같은 함수에 `risk_queue.Queue`를 주입할 수 있다. 이 adapter는 같은 path lease와 Git ref의 fast-forward를 검사한다. 소비에서는 immutable checkpoint를 한 commit에서 최대 6개 reader로 읽고 기존 전역 API 속도 제한을 공유한다.

## 발송과 장애 복구

소비는 checkpoint의 ID·본문/정책 해시, 현재 ready owner, result 해시/전체 내용을 대조하고 모두 같은 SHA 조건으로 pending→sending을 잡는다. 이후 `review_receipt`와 기존 outbox/수신자 영수증을 보존한다. 접수 후 seen push/ACK 복구는 고정 receipt/result로 처리하며 이후 owner 변화 때문에 재분석하거나 accepted를 다시 발송하지 않는다. DATA 응답 유실과 부분 접수는 기존 잠금·복구 계약을 유지한다.

진단 metrics/filter-log/preview/log는 SMTP·seen·ACK 뒤 저장한다. 충돌 원본을 덮어쓰지 않고 실패는 로컬 private spool과 경고에 남긴다. 원장·outbox·SMTP 영수증·ACK는 필수 저장이며 이 완화에 포함되지 않는다.

정리는 발행/종결 각각 **12시간 초과** 기준이다. 250개 삭제마다 진행 metadata를 함께 commit한다. 입력 shards를 먼저 정리하고 results/outbox/preview/log와 최종 정리 표시는 마지막 transaction에서만 처리한다. ACK 자동 정리는 각 종류 1묶음으로 제한하며 명시적 maintenance는 남은 전체를 이어간다. sending/uncertain/부분 종결·시각 불명·복구/중복방지 자료 보존은 기존과 같다.

## 적용 전후 확인

새 정책 hash는 새 수집 회차부터 고정된다. 병합 직전 실제 private index·원장·outbox·Actions를 재조회해 미종결 구형 계약/복구 작업이 없음을 확인한다. 기존 회차가 살아 있으면 그 고정 코드로 종결/복구한 뒤 적용한다. 정책 hash를 바꿔 진행 중 회차를 통과시키지 않는다.

정규 자연 실행에서 input 전체 판단·미시도 0, 시작/결과/소비 시각, SMTP 접수·seen·ACK를 같은 회차 ID로 확인해야 운영 개선 판정이 가능하다. 오프라인 함수 시간은 AI 분석 시간이나 실제 안내 지연이 아니다. 예약이 실행되지 않는 외부 원인은 저장소 코드만으로 해결됐다고 판정할 수 없다. 예약/배포 승인은 운영자의 명시적 범위에 따른다.

새 회차의 `first_pass_complete/first_pass_count`는 전체 입력 1차 판단 여부/건수이며 `analysis_complete`는 보류 해소 여부다. 본문 상태 보류를 포함한 전수 판단 완료와 모든 사실 확인 완료를 구분한다. `collection_timing.input_ready_at`은 입력 준비 시각이고 private 큐 게시 완료는 Actions의 collected/step 종료 증거로 확인한다.
