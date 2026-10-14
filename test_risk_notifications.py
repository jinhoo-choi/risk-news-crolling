"""Failed history/ACK notification stays owner-only and avoids receipt mutations."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
import risk_subscription as bridge
from risk_owner_verification import OWNER


class Notification(unittest.TestCase):
    def test_owner_notification_confirms_acceptance_without_public_details(self):
        bot=MagicMock(); bot.send_email_error.return_value=True
        capture=io.StringIO()
        with patch.dict(os.environ,{'EMAIL_PASSWORD':'offline','GITHUB_RUN_ID':'123'}), \
             patch.object(bridge,'import_bot',return_value=bot), \
             patch.object(bridge,'Queue',side_effect=AssertionError('queue write forbidden')), \
             contextlib.redirect_stdout(capture):
            self.assertTrue(bridge.notify_error(RuntimeError('private source'), 'private context'))
        self.assertEqual(bot.NO_RESULT_RECEIVER,OWNER)
        bot.send_email_error.assert_called_once()
        self.assertIn('actions/runs/123',bot.send_email_error.call_args.args[1])
        self.assertNotIn('private',capture.getvalue())
        self.assertNotIn(OWNER,capture.getvalue())
        self.assertIn('SMTP accepted 1건',capture.getvalue())
    def test_unconfirmed_notification_never_claims_accepted(self):
        bot=MagicMock();bot.send_email_error.return_value=False
        capture=io.StringIO()
        with patch.dict(os.environ,{'EMAIL_PASSWORD':'offline'}), \
             patch.object(bridge,'import_bot',return_value=bot),contextlib.redirect_stdout(capture):
            self.assertFalse(bridge.notify_error(RuntimeError('offline')))
        self.assertNotIn('SMTP accepted',capture.getvalue())
    def test_missing_credentials_cannot_send(self):
        with patch.dict(os.environ,{'EMAIL_PASSWORD':''}),patch.object(bridge,'import_bot') as loader:
            self.assertFalse(bridge.notify_error(RuntimeError('offline')))
            loader.assert_not_called()
    def test_notify_command_does_not_read_queue_or_resend_delivery(self):
        previous=Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp);Path('risk_subscription_config.json').write_text('{"engine":"subscription"}')
                with patch('sys.argv',['risk_subscription.py','notify-error']), \
                     patch.object(bridge,'notify_error') as notify, \
                     patch.object(bridge,'Queue',side_effect=AssertionError('receipt mutation forbidden')):
                    bridge.main();notify.assert_called_once()
            finally:os.chdir(previous)

if __name__=='__main__':unittest.main()
