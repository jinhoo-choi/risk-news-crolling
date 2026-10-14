"""옛 수동 경로가 구독 운영의 API0 정책을 우회하지 못하는지 확인."""
import json
import tempfile
import unittest
from pathlib import Path

from risk_legacy_api_preflight import legacy_api_enabled

ROOT = Path(__file__).parent
UNPAID = ('diag_keyword_volume.yml', 'verify_send.yml', 'diag_exposure.yml',
          'test_send_self.yml', 'manual_send.yml')


class LegacyPreflightTests(unittest.TestCase):
    def test_only_explicit_api_setting_allows_legacy_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'config.json'
            for engine in ('subscription', 'paused', 'api', None, 'unknown'):
                p.write_text(json.dumps({'engine': engine}))
                with self.subTest(engine=engine):
                    self.assertEqual(legacy_api_enabled(p), engine == 'api')

    def test_missing_malformed_or_nonobject_config_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'config.json'
            self.assertFalse(legacy_api_enabled(p))
            for text in ('{', 'null', '[]', '"api"'):
                p.write_text(text)
                with self.subTest(text=text):
                    self.assertFalse(legacy_api_enabled(p))

    def test_unpaid_legacy_tools_have_no_paid_secret_and_block_paid_calls(self):
        for name in UNPAID:
            text = (ROOT / '.github/workflows' / name).read_text()
            with self.subTest(workflow=name):
                self.assertNotIn('secrets.ANTHROPIC_API_KEY', text)
                self.assertIn("RISK_ENGINE: 'subscription'", text)

    def test_legacy_full_pipeline_checks_config_before_credentials_or_ai(self):
        text = (ROOT / '.github/workflows/test_full_pipeline.yml').read_text()
        check = text.index('run: python risk_legacy_api_preflight.py')
        self.assertLess(check, text.index('secrets.ANTHROPIC_API_KEY'))
        self.assertLess(check, text.index('run: python3 naver_news_monitor.py'))
        self.assertNotIn('continue-on-error', text[:check])
        self.assertNotIn('if: always()', text[:text.index('run: python3 naver_news_monitor.py')])


if __name__ == '__main__':
    unittest.main()
