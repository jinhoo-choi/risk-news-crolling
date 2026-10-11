# 2026-10-07 Actions Node 24 최소 호환 변경

기준 main: `bae545b7af1f001f3fcc165d5d5ca509efd11518`. 변경은 검토용 PR; 운영 workflow 및 실발송 미실행.

## 2026-10-11 최신 main 재검증 (PR #7)

- 통합 기준 main: `576a6d736c391deb56f05805f26bcf8e93345194`. 기존 PR head에 main을 병합하는 방식으로 이력 보존.
- 최신 main 통합 충돌 없음. 기지사건 등급·가격 경고 및 공지 크롤러 변경을 보존했습니다.
- 최신 main baseline과 통합본을 각각 검증: bash run_tests.sh 컴파일 및 11개 회귀/시뮬레이션 통과, check_changes.py 통과. 기존 미사용 심볼 경고 유지. 로컬 Python 3.12.14; hosted Python 3.11 명시.
- 테스트는 비밀 환경변수 없이 실행하고 실제 socket 연결을 차단했습니다. 실제 연결 시도 0건; 유료 API·운영 발송·네이버 저장/발행 미실행.
- 세 저장소 17개 workflow/18개 job: actionlint 1.7.12, hosted Bash 70개 syntax, YAML 의미 비교 통과. 최신 main과의 차이는 원래 PR의 action 버전, risk Node 강제 플래그 제거, community verify OS 고정뿐입니다. 트리거·권한·secret 참조·실행 명령·입력·Python minor·cache/artifact 설정은 보존.
- Node24 action.yml 및 허용 입력, hosted runner/Python은 새 PR CI 로그로 따로 확인합니다. 오프라인 결과가 Ubuntu 26.04·운영 실행 성공을 뜻하지 않습니다.
- 롤백: main 병합 커밋으로 추가 통합한 내용은 기존 PR head로 복귀 가능. 운영 main 미병합 상태이므로 운영 변경 롤백은 발생하지 않았습니다.

## 현재 workflow / job 목록

| Workflow | Job | 기존 runner → 변경 | 기존 action → 변경 |
|---|---|---|---|
| dart_mapper.yml | group-mapper | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| ci.yml | tests | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| verify_send.yml | verify | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| test_send_self.yml | test-send-self | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| manual_send.yml | manual-send | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| test_full_pipeline.yml | test-full-pipeline | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| diag_keyword_volume.yml | diag | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| diag_exposure.yml | diag | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| news_monitor.yml | crawl-broker-notices | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6<br>actions/upload-artifact@v4 → actions/upload-artifact@v6 |
| news_monitor.yml | crawl-and-notify | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6<br>actions/cache@v4 → actions/cache@v5<br>actions/upload-artifact@v4 → actions/upload-artifact@v6<br>actions/upload-artifact@v4 → actions/upload-artifact@v6 |

## 확인 근거와 선택

- news monitor #548 / run 37574180757: 두 hosted job 모두 runner 2.337.0, Ubuntu 24.04. broker job은 checkout/setup-python/upload-artifact, notify job은 여기에 cache@v4까지 Node 20 경고 실측.
- 모든 job에 Python 3.11 명시. requirements 및 yfinance의 pandas/numpy/curl_cffi/rapidfuzz 의존성은 pip wheel 경로 사용; 별도 apt, Chrome/Chromium, 시스템 Python, 24.04 전용 바이너리 경로 사용 없음. Ubuntu 26.04 공식 toolcache에 3.11.16 존재. 확인된 OS 전용 의존성이 없어 ubuntu-latest 유지.
- news_monitor.yml의 FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 제거: action 자체가 node24를 선언하므로 강제 실행 플래그 불필요.
- bash run_tests.sh의 기존 11개 오프라인 회귀·시뮬레이션 전부 통과. 변형 오탐 46/46 차단, 정탐 33/33 통과, 원본 미탐 0/39. 최초 로컬 의존성 누락은 별도 테스트 환경에 기존 requirements 설치 후 해결; 저장소 의존성 변경 없음.

## 불변식 / 검증 범위

트리거, schedule, concurrency, permissions, secrets, if 조건, action 입력, Python 버전, cache key/path, artifact name/path/retention, 실행 명령 및 발송 가드는 유지. YAML 파싱 후 이 항목의 변경 전후 동등성 확인.

3개 저장소 17개 workflow / 18개 job 정적 점검. actionlint 1.7.12 통과(기존 self-hosted 사용자 label naver-blog는 외부 검증 설정에 등록, shellcheck/pyflakes 연동 제외); hosted Bash 블록 69개 bash -n 통과. 공식 action.yml로 전체 44개 action 참조의 입력 키 검증 및 hosted 42개 참조 node24 선언 확인. Windows 2개 참조만 보류.

26.04 실제 실행·브라우저 실행·운영 dispatch·재실행·유료 호출·실발송은 하지 않음. 기존 테스트는 로컬 Python 3.12.14 환경; hosted Python 3.11/3.12 런타임이나 Ubuntu 26.04에서의 실행 성공을 의미하지 않음.

## 공식 자료

- https://github.com/actions/runner-images/issues/14748 — 2026-10-19 시작, 2026-11-19 완료 예정.
- https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md
- https://github.com/actions/checkout/blob/v5/action.yml
- https://github.com/actions/setup-python/blob/v6/README.md
- https://github.com/actions/cache/blob/v5/README.md
- https://github.com/actions/upload-artifact/blob/v6/README.md
- https://github.com/actions/download-artifact/blob/v7/README.md

## 다음 자연 실행의 확인 / 롤백

병합 후 정상 일정의 Set up job runner 버전, Node 20 경고 제거, Python 설치, pip 설치, artifact 업로드를 확인. 운영 검증 완료 주장 없음. 문제 발생 시 이 PR의 변경 커밋만 revert. Windows self-hosted는 설치 버전 확인 전 기존 action 유지.
