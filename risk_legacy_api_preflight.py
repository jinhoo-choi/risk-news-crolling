"""예전 유료 API 테스트는 명시적 API 운영 설정일 때만 실행한다."""
import json
import sys
from pathlib import Path


def legacy_api_enabled(path='risk_subscription_config.json'):
    try:
        config = json.loads(Path(path).read_text(encoding='utf-8'))
        return isinstance(config, dict) and config.get('engine') == 'api'
    except (OSError, ValueError):
        return False


def main():
    if not legacy_api_enabled():
        print('구독·일시중지·설정 미확인: 기존 유료 API 테스트 실행 금지', file=sys.stderr)
        return 1
    print('명시적 API 운영 설정 확인')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
