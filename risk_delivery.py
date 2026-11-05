"""Durable per-recipient SMTP reservations; no retry after uncertain DATA."""
import hashlib
import smtplib
import sys
import uuid
from email import message_from_string
from email.policy import SMTP
from email.utils import getaddresses

from risk_queue import now
from risk_audit import checkpoint, record_error


def address_key(address):
    return hashlib.sha256(address.encode()).hexdigest()


def report_headers(targets, roles):
    """Resolve the approved three To and two Cc keys against live addresses."""
    if not isinstance(roles, dict):
        raise RuntimeError('업무 보고 수신 역할 설정 누락')
    to, cc = roles.get('to'), roles.get('cc')
    if not isinstance(to, list) or len(to) != 3 or not isinstance(cc, list) or len(cc) != 2:
        raise RuntimeError('업무 보고 To/Cc 인원 불일치')
    by_key = {address_key(address): address for address in targets}
    if len(set(to + cc)) != 5 or set(to + cc) != set(by_key):
        raise RuntimeError('승인된 업무 보고 수신자 불일치')
    return [by_key[k] for k in to], [by_key[k] for k in cc]


def close_smtp(server, primary=None):
    """Connection teardown must never replace an earlier DATA/login failure."""
    try:
        server.quit()
    except Exception as quit_error:
        record_error('cleanup', quit_error, 'smtp_quit_failed')
        try:
            server.close()
        except Exception as close_error:
            record_error('cleanup', close_error, 'smtp_close_failed')
            if primary is None:
                raise close_error from quit_error


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

    def send_report(self, sender, password, message):
        """One envelope and one DATA. A pre-DATA disconnect can safely reconnect."""
        headers = [addr for field in ('To', 'Cc')
                   for _, addr in getaddresses(message.get_all(field, []))]
        if headers != self.targets or message.get('Bcc'):
            raise RuntimeError('보고 헤더와 고정 수신자 불일치')
        wire = message.as_bytes(policy=SMTP)
        # Resume only the original frozen message and only recipients never attempted.
        job, _ = self.read()
        pending = [a for a in self.targets if address_key(a) not in job.get('receipts', {})]
        if not pending:
            return
        for attempt in range(2):
            self.check_config()
            checkpoint('smtp_connect')
            server = None
            try:
                server = self.real_smtp('smtp.gmail.com', 465, timeout=30)
                server.ehlo()
                server.login(sender, password)
                code, _ = server.mail(sender)
                if code != 250:
                    raise RuntimeError(f'SMTP MAIL FROM 거부 ({code}) — DATA 미시도')
                ready, refused = [], []
                for address in pending:
                    code, _ = server.rcpt(address)
                    if code in (250, 251):
                        ready.append(address_key(address))
                    elif code == 421:
                        raise smtplib.SMTPServerDisconnected('RCPT 421 before DATA')
                    else:
                        refused.append(address_key(address))
                if not ready:
                    raise RuntimeError('모든 RCPT 거부 — DATA 미시도')
            except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError) as error:
                if server is not None:
                    server.close()
                record_error('smtp_connect', error, 'smtp_disconnected_before_data')
                if attempt == 0:
                    continue
                raise
            except Exception:
                if server is not None:
                    close_smtp(server, sys.exc_info()[1])
                raise
            break

        def reserve(job):
            expected = [address_key(a) for a in self.targets]
            if (job.get('status') != 'sending' or job.get('expected_receipts') != expected
                    or set(job['receipts']) != set(expected) - set(ready + refused)
                    or any(state != 'accepted' for state in job['receipts'].values())
                    or job.get('attempted_count', 0) != len(job['receipts'])):
                raise RuntimeError('이미 시도한 수신자 재전송 금지')
            job['receipts'].update(dict.fromkeys(ready, 'sending'))
            job['receipts'].update(dict.fromkeys(refused, 'rejected'))
            job['attempted_count'] = len(job['receipts'])

        def record(job, state):
            if job.get('status') != 'sending' or any(job['receipts'].get(k) != 'sending' for k in ready):
                raise RuntimeError('SMTP 선기록 변경 — 발송 상태 확인 필요')
            job['receipts'].update(dict.fromkeys(ready, state))
            if state != 'accepted' or refused:
                job['status'] = 'uncertain'

        try:
            self.check_config()
            checkpoint('smtp_data')
            self.change(reserve)
            try:
                self.smtp_calls += 1
                code, _ = server.data(wire)
            except smtplib.SMTPDataError as error:
                code = error.smtp_code
            except Exception as error:
                record_error('smtp_data', error, 'smtp_data_unconfirmed')
                self.change(lambda j: record(j, 'uncertain'))
                raise RuntimeError('SMTP DATA 응답 미확인 — 재발송 금지') from error
            state = 'accepted' if code == 250 else 'rejected'
            self.change(lambda j: record(j, state))
            checkpoint('smtp_receipt')
            if refused or state != 'accepted':
                raise RuntimeError('SMTP 일부 또는 전원 거부 — 이력·ACK 보류')
        finally:
            close_smtp(server, sys.exc_info()[1])

    def factory(self, host, port, **kwargs):
        if (host, port) != ('smtp.gmail.com', 465):
            raise RuntimeError('허용하지 않은 SMTP 서버')
        checkpoint('smtp_connect')
        server = self.real_smtp(host, port, timeout=30)
        guard = self
        class Session:
            def __enter__(self):
                return self
            def __exit__(self, typ, value, trace):
                close_smtp(server, value)
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
