"""Durable per-recipient SMTP reservations; no retry after uncertain DATA."""
import hashlib
import uuid
from email import message_from_string
from email.utils import getaddresses

from risk_queue import now
from risk_audit import checkpoint, record_error


def address_key(address):
    return hashlib.sha256(address.encode()).hexdigest()


class Delivery:
    def __init__(self, queue, job_id, real_smtp, check_config, resume=False):
        self.queue, self.path = queue, 'jobs/'+job_id+'.json'
        self.real_smtp, self.check_config = real_smtp, check_config
        self.targets = []
        self.resume = resume
        self.smtp_calls = 0

    def read(self):
        return getattr(self.queue, 'get_fresh', self.queue.get)(self.path)

    def change(self, fn):
        job, sha = self.read()
        if not job or not sha:
            raise RuntimeError('전송 선기록 읽기 실패')
        fn(job)
        # Makes a confirmed write distinguishable from another worker's reserve.
        job['delivery_revision'] = uuid.uuid4().hex
        self.queue.put(self.path, job, sha)
        return job

    def prepare(self, targets):
        if self.targets:
            raise RuntimeError('동일 회차 추가 메일 금지')
        self.targets = list(dict.fromkeys(targets))
        if not self.targets:
            raise RuntimeError('수신자 없음')
        keys = [address_key(t) for t in self.targets]
        def prepare(job):
            if job.get('status') != 'sending':
                raise RuntimeError('발송 상태 변경 — 수신자 준비 중단')
            if self.resume:
                if (keys != job.get('expected_receipts')
                        or any(v != 'accepted' for v in job.get('receipts', {}).values())
                        or not set(job.get('receipts', {})) <= set(keys)
                        or job.get('attempted_count', 0) != len(job.get('receipts', {}))):
                    raise RuntimeError('기존 수신자·접수 상태 불일치 — 복구 금지')
            elif job.get('receipts') or job.get('attempted_count', 0):
                raise RuntimeError('발송 시작 수신자 재준비 금지')
            job['expected_receipts'] = keys
        self.change(prepare)

    def complete(self):
        job, _ = self.read()
        expected = set(job.get('expected_receipts', []))
        receipts = job.get('receipts', {})
        if not expected or set(receipts) != expected or any(v != 'accepted' for v in receipts.values()):
            raise RuntimeError('수신자 전원 접수 확인 전 이력 저장 금지')
        return job

    def factory(self, host, port, **kwargs):
        if (host, port) != ('smtp.gmail.com', 465):
            raise RuntimeError('허용하지 않은 SMTP 서버')
        checkpoint('smtp_connect')
        server = self.real_smtp(host, port, timeout=30)
        guard = self
        class Session:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                try:
                    server.quit()
                except Exception:
                    server.close()
            def ehlo(self):
                return server.ehlo()
            def login(self, *args):
                return server.login(*args)
            def sendmail(self, sender, recipients, raw):
                if len(recipients) != 1 or recipients[0] not in guard.targets:
                    raise RuntimeError('기존 수신자 경로 외 전송 금지')
                addr = recipients[0]
                msg = message_from_string(raw)
                if [a for _, a in getaddresses(msg.get_all('To', []))] != [addr] or msg.get('Cc') or msg.get('Bcc'):
                    raise RuntimeError('SMTP envelope과 헤더 불일치')
                guard.check_config()
                key = address_key(addr)
                if guard.resume:
                    previous, _ = guard.read()
                    if previous.get('status') != 'sending':
                        raise RuntimeError('복구 상태 변경 — 발송 중단')
                    if previous.get('receipts', {}).get(key) == 'accepted':
                        return {}  # No SMTP DATA for already accepted recipients.
                def reserve(job):
                    if job.get('status') != 'sending' or key in job.get('receipts', {}):
                        raise RuntimeError('시작·접수·미확인 수신자 재전송 금지')
                    job.setdefault('receipts', {})[key] = 'sending'
                    job['attempted_count'] = job.get('attempted_count', 0)+1
                checkpoint('smtp_data')  # Diagnostics may fail only BEFORE reservation/DATA.
                guard.change(reserve)
                try:
                    guard.smtp_calls += 1
                    refused = server.sendmail(sender, recipients, raw)
                except Exception as error:
                    record_error('smtp_data', error, 'smtp_data_unconfirmed')
                    try:
                        guard.change(lambda j: (j['receipts'].__setitem__(key, 'uncertain'), j.update(status='uncertain')))
                    except Exception as cleanup:
                        record_error('cleanup', cleanup, 'smtp_uncertain_write_failed')
                    raise RuntimeError('SMTP DATA 접수 불확실 — 자동 재시도 금지') from error
                state = 'rejected' if refused else 'accepted'
                def record(job):
                    if job.get('status') != 'sending' or job.get('receipts', {}).get(key) != 'sending':
                        raise RuntimeError('접수 선기록 변경 — 자동 복구 금지')
                    job['receipts'][key] = state
                    if refused:
                        job['status'] = 'uncertain'
                # Persist the accepted/rejected receipt BEFORE any diagnostic
                # write that could fail. An SMTP success must never be lost to logging.
                guard.change(record)
                checkpoint('smtp_receipt')
                if refused:
                    raise RuntimeError('SMTP 수신자 거부 — 이력 저장·자동 재시도 금지')
                return refused
        return Session()
