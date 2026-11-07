"""Durable per-recipient SMTP reservations; no retry after uncertain DATA."""
import hashlib
import smtplib
import sys
import uuid
from datetime import datetime, timedelta
from email import message_from_string
from email.policy import SMTP
from email.utils import getaddresses

from risk_queue import now, digest
from risk_audit import checkpoint, record_error


def address_key(address):
    return hashlib.sha256(address.encode()).hexdigest()


def rejection_evidence(job, packet_hash=None):
    """Replay complete, durable attempts; old rejected/uncertain receipts are not proof."""
    proof = job.get('smtp_evidence', {})
    attempts = proof.get('attempts', [])
    expected = job.get('expected_receipts', [])
    if (proof.get('version') != 1 or not proof.get('packet_hash')
            or packet_hash is not None and proof['packet_hash'] != packet_hash
            or not isinstance(attempts, list) or not 1 <= len(attempts) <= 2
            or not expected or len(expected) != len(set(expected))):
        raise RuntimeError('SMTP 거부 증거·고정본 불일치')
    states, outcomes = {}, {}
    for number, attempt in enumerate(attempts, 1):
        targets = attempt.get('targets', [])
        answers = attempt.get('outcomes', {})
        if (attempt.get('number') != number or not attempt.get('finished_at')
                or not attempt.get('message_hash') or not targets
                or len(targets) != len(set(targets)) or not set(targets) <= set(expected)
                or set(answers) != set(targets)
                or any(states.get(key) == 'accepted' for key in targets)
                or number == 1 and set(targets) != set(expected)
                or number > 1 and any(key not in outcomes or outcomes[key]['state'] != 'rejected'
                    or outcomes[key]['stage'] != 'before_data' and not 400 <= outcomes[key]['code'] < 500
                    for key in targets)):
            raise RuntimeError('SMTP 미확정·중복 시도 — 재발송 금지')
        for key, answer in answers.items():
            state, code, stage = answer.get('state'), answer.get('code'), answer.get('stage')
            if state == 'accepted':
                valid = stage == 'data' and code == 250
            elif state == 'rejected':
                valid = (stage in {'connect', 'ehlo', 'mail', 'rcpt', 'data', 'auth'} and type(code) is int and 400 <= code < 600
                         or stage == 'before_data' and code is None and answer.get('reason') == 'disconnected')
            else:
                valid = False
            if not valid:
                raise RuntimeError('SMTP 접수 불확실 — 재발송 금지')
            states[key], outcomes[key] = state, answer
    if (states != job.get('receipts') or set(states) != set(expected)
            or job.get('attempted_count') != len(states)):
        raise RuntimeError('SMTP 영수증과 시도 증거 불일치')
    return outcomes


def retry_targets(job, packet_hash=None, ready=True):
    outcomes = rejection_evidence(job, packet_hash)
    proof = job['smtp_evidence']
    if len(proof['attempts']) >= 2:
        return []
    # All new attempts remain inside the original TTL, including partial delivery.
    expiry = datetime.fromisoformat(job['expires_at'].replace('Z', '+00:00'))
    if expiry.tzinfo is None or expiry <= now():
        return []
    if ready and datetime.fromisoformat(proof['attempts'][-1]['retry_at']) > now():
        return []
    return [key for key, item in outcomes.items() if item['state'] == 'rejected'
            and (item['stage'] == 'before_data' or 400 <= item['code'] < 500)]


def checked_delivery_failure(job):
    """A failed run can release collection only with retained zero-acceptance proof."""
    outcomes = rejection_evidence(job)
    closure = job.get('delivery_failure', {})
    ids = closure.get('carried_ids')
    count = job.get('input_count', len(job.get('input', {}).get('items', [])))
    if (job.get('status') != 'delivery_failed' or any(v['state'] != 'rejected' for v in outcomes.values())
            or closure.get('evidence_hash') != digest(job['smtp_evidence'])
            or closure.get('input_hash') != job.get('input_hash')
            or closure.get('body_manifest_hash') != job.get('body_manifest_hash')
            or not isinstance(ids, list) or len(ids) != count or len(ids) != len(set(ids))
            or job.get('ack') or job.get('history_saved')):
        raise RuntimeError('미접수 실패 종결 증거 불일치')


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
        self.revision = job['delivery_revision']
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
                        or any(v != 'accepted' for v in job.get('receipts', {}).values()) and not retry_targets(job)
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

    def send_report(self, sender, password, message, packet_hash):
        """Retry only proven non-delivery; reserve before SMTP, persist before retry."""
        headers = [addr for field in ('To', 'Cc')
                   for _, addr in getaddresses(message.get_all(field, []))]
        if headers != self.targets or message.get('Bcc'):
            raise RuntimeError('보고 헤더와 고정 수신자 불일치')
        wire = message.as_bytes(policy=SMTP)
        while True:
            self.check_config()
            previous, _ = self.read()
            receipts = previous.get('receipts', {})
            if receipts:
                keys = retry_targets(previous, packet_hash)
            else:
                keys = [address_key(a) for a in self.targets]
            if not keys:
                if all(v == 'accepted' for v in receipts.values()) and receipts:
                    return
                raise RuntimeError('SMTP 거부 — 재시도 대기·상한·만료 또는 영구 거부')
            number = len(previous.get('smtp_evidence', {}).get('attempts', [])) + 1
            def reserve(job):
                if (job.get('status') not in {'sending', 'delivery_rejected'}
                        or job.get('receipts', {}) != receipts
                        or job.get('delivery_revision') != previous.get('delivery_revision')
                        or job.get('expected_receipts') != [address_key(a) for a in self.targets]):
                    raise RuntimeError('SMTP 동시 시도·수신자 변경 — 발송 금지')
                if receipts and keys != retry_targets(job, packet_hash):
                    raise RuntimeError('SMTP 재시도 조건 변경')
                proof = job.setdefault('smtp_evidence', {'version': 1, 'packet_hash': packet_hash, 'attempts': []})
                if proof['packet_hash'] != packet_hash or len(proof['attempts']) != number-1 or number > 2:
                    raise RuntimeError('SMTP 고정본·시도 상한 불일치')
                proof['attempts'].append({'number': number, 'targets': keys,
                    'message_hash': hashlib.sha256(wire).hexdigest(), 'started_at': now().isoformat()})
                job.setdefault('receipts', {}).update(dict.fromkeys(keys, 'sending'))
                job.update(status='sending', attempted_count=len(job['receipts']))
            checkpoint('smtp_connect')
            self.change(reserve)
            server, stage, data_started = None, 'connect', False
            answers, ready = {}, []
            def rejected(stage, code, reason=None):
                return dict(state='rejected', stage=stage, code=code, **({'reason': reason} if reason else {}))
            try:
                try:
                    server = self.real_smtp('smtp.gmail.com', 465, timeout=30)
                    stage = 'ehlo'
                    greeting = server.ehlo()
                    if isinstance(greeting, tuple) and greeting[0] != 250:
                        raise smtplib.SMTPHeloError(*greeting)
                    stage = 'auth'
                    server.login(sender, password)
                    stage = 'mail'
                    code, _ = server.mail(sender)
                    if code != 250:
                        raise smtplib.SMTPSenderRefused(code, b'refused', sender)
                    stage = 'rcpt'
                    for address in self.targets:
                        key = address_key(address)
                        if key not in keys:
                            continue
                        code, _ = server.rcpt(address)
                        if code in (250, 251):
                            ready.append(key)
                        elif 400 <= code < 600:
                            answers[key] = rejected('rcpt', code)
                            if code == 421:
                                raise smtplib.SMTPResponseException(code, b'connection closing')
                        else:
                            raise RuntimeError('알 수 없는 RCPT 응답')
                    if ready:
                        self.check_config()
                        current, _ = self.read()
                        if (current.get('delivery_revision') != self.revision
                                or current.get('status') != 'sending'
                                or datetime.fromisoformat(current['expires_at'].replace('Z', '+00:00')) <= now()):
                            raise RuntimeError('SMTP DATA 직전 소유권·유효시간 변경')
                        checkpoint('smtp_data')
                        stage, data_started = 'data', True
                        self.smtp_calls += 1
                        try:
                            code, _ = server.data(wire)
                        except smtplib.SMTPDataError as error:
                            code = error.smtp_code
                        for key in ready:
                            answers[key] = ({'state': 'accepted', 'stage': 'data', 'code': 250} if code == 250
                                            else rejected('data', code) if 400 <= code < 600
                                            else {'state': 'uncertain', 'stage': 'data', 'code': code})
                except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError):
                    for key in keys:
                        answers.setdefault(key, {'state': 'uncertain', 'stage': 'data', 'code': None} if data_started
                                           else rejected('before_data', None, 'disconnected'))
                except smtplib.SMTPResponseException as error:
                    for key in keys:
                        answers.setdefault(key, rejected(stage, error.smtp_code))
                except Exception:
                    # Unknown failures, including a changed lease, never create retry proof.
                    raise
                # Immediate reconnect only for a pre-DATA disconnect/421. Other 4xx
                # retries use the existing follow-up run after a 30-minute delay.
                immediate = not data_started and all(v.get('code') == 421 or v.get('stage') == 'before_data'
                                                    for v in answers.values())
                def record(job):
                    attempt = job['smtp_evidence']['attempts'][-1]
                    if (job.get('status') != 'sending' or attempt['number'] != number
                            or job.get('delivery_revision') != self.revision
                            or attempt.get('finished_at') or set(answers) != set(keys)
                            or any(job['receipts'].get(k) != 'sending' for k in keys)):
                        raise RuntimeError('SMTP 선기록 변경 — 접수 상태 확인 필요')
                    attempt.update(outcomes=answers, finished_at=now().isoformat(),
                                   retry_at=(now()+timedelta(minutes=0 if immediate else 30)).isoformat())
                    job['receipts'].update({k: v['state'] for k, v in answers.items()})
                    job['status'] = ('uncertain' if any(v == 'uncertain' for v in job['receipts'].values())
                                     else 'delivery_rejected' if 'rejected' in job['receipts'].values() else 'sending')
                final = self.change(record)
                checkpoint('smtp_receipt')
            finally:
                if server is not None:
                    # QUIT failures cannot erase an already received DATA reply.
                    try:
                        close_smtp(server, sys.exc_info()[1])
                    except Exception as error:
                        record_error('cleanup', error, 'smtp_close_failed')
            if final['status'] == 'uncertain':
                raise RuntimeError('SMTP DATA 응답 미확인 — 재발송 금지')
            if final['status'] == 'sending':
                return
            if not immediate or not retry_targets(final, packet_hash):
                raise RuntimeError('SMTP 거부 — 증거 보존, 제한 복구 또는 실패 종결 필요')

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
                checkpoint('smtp_data')  # Diagnostic only; durable reservation below still gates DATA.
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
