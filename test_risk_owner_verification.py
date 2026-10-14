import copy
from datetime import timedelta
from unittest import TestCase, main
from email.mime.text import MIMEText

from risk_owner_verification import OwnerSMTP, OWNER, require_pending
from risk_queue import now


class Queue:
    def __init__(self):
        self.job = {'mode': 'verification', 'status': 'pending', 'receipts': {},
                    'expires_at': (now()+timedelta(minutes=5)).isoformat()}
    def get(self, path):
        return copy.deepcopy(self.job), 'sha'
    def put(self, path, value, sha=None):
        self.job = copy.deepcopy(value)


class SMTP:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0
    def sendmail(self, sender, recipients, raw):
        self.calls += 1
        if self.fail:
            raise ConnectionError('DATA response unknown')
        return {}
    def quit(self):
        pass


def message(to=OWNER, bcc=None):
    m = MIMEText('test', 'html')
    m['To'] = to
    if bcc:
        m['Bcc'] = bcc
    return m.as_string()


class IsolationTests(TestCase):
    def setUp(self):
        self.queue, self.smtp = Queue(), SMTP()
        self.guard = OwnerSMTP(lambda *a, **k: self.smtp, self.queue, 'job')
    def test_accepted_is_reserved_and_recorded_once(self):
        with self.guard.factory('smtp.gmail.com', 465) as server:
            server.sendmail('sender@example.invalid', [OWNER], message())
        self.assertEqual(self.smtp.calls, 1)
        self.assertEqual(list(self.queue.job['receipts'].values()), ['accepted'])
        self.assertEqual(self.queue.job['status'], 'sending')
        self.assertEqual(self.guard.accepted, 1)
        with self.assertRaises(RuntimeError):
            self.guard.factory('smtp.gmail.com', 465)
    def test_ambiguous_data_locks_without_retry(self):
        self.smtp.fail = True
        with self.assertRaises(RuntimeError):
            with self.guard.factory('smtp.gmail.com', 465) as server:
                server.sendmail('sender@example.invalid', [OWNER], message())
        self.assertEqual(self.queue.job['status'], 'uncertain')
        self.assertEqual(list(self.queue.job['receipts'].values()), ['uncertain'])
        with self.assertRaises(RuntimeError):
            self.guard.factory('smtp.gmail.com', 465)
        self.assertEqual(self.smtp.calls, 1)
    def test_group_envelope_is_rejected_before_attempt(self):
        with self.assertRaises(RuntimeError):
            with self.guard.factory('smtp.gmail.com', 465) as server:
                server.sendmail('sender@example.invalid', ['group@example.invalid'], message())
        self.assertEqual(self.smtp.calls, 0)
        self.assertEqual(self.queue.job['status'], 'pending')
    def test_header_drift_rejected(self):
        for raw in (message('group@example.invalid'), message(bcc='group@example.invalid')):
            with self.assertRaises(RuntimeError):
                with self.guard.factory('smtp.gmail.com', 465) as server:
                    server.sendmail('sender@example.invalid', [OWNER], raw)
        self.assertEqual(self.smtp.calls, 0)
    def test_expired_or_started_job_rejected(self):
        for status in ('sending', 'uncertain', 'delivery_complete', 'verified', 'completed'):
            self.queue.job['status'] = status
            with self.assertRaises(RuntimeError):
                require_pending(self.queue.job)
        self.queue.job['status'] = 'pending'
        self.queue.job['expires_at'] = (now()-timedelta(seconds=1)).isoformat()
        with self.assertRaises(RuntimeError):
            require_pending(self.queue.job)
    def test_production_job_cannot_be_self_verified(self):
        self.queue.job['mode'] = 'production'
        with self.assertRaises(RuntimeError):
            require_pending(self.queue.job)


if __name__ == '__main__':
    main()
