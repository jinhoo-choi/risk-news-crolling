"""Private execution evidence. Never grants delivery permission or retries SMTP."""
import contextvars
import copy
import json
import os
import re
import traceback
import uuid
from pathlib import Path

from risk_queue import JOB_ID, Queue, now

ACTIVE = contextvars.ContextVar('risk_audit', default=None)
STAGES = {'started', 'config', 'collect', 'search', 'body', 'collected', 'verify',
          'render', 'outbox', 'smtp_prepare', 'smtp_connect', 'smtp_data', 'smtp_receipt',
          'history_local', 'delivery_state', 'ack_verify', 'ack_commit', 'ack_complete',
          'cleanup', 'owner_notification', 'request', 'request_summary', 'workflow_finalize'}


def redact(value):
    text = str(value)
    for key, secret in os.environ.items():
        if secret and len(secret) >= 4 and any(k in key.upper() for k in ('TOKEN', 'PASSWORD', 'SECRET', 'API_KEY')):
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=_.,:-]+', '[AUTH REDACTED]', text)
    text = re.sub(r'(?i)((?:password|token|cookie|secret|api[_-]?key)[\"\x27]?\s*[:=]\s*)[^\s,;]+',
                  r'\1[REDACTED]', text)
    return text


def error_detail(error):
    detail = {'type': type(error).__name__, 'message': redact(error),
              'traceback': redact(''.join(traceback.format_exception(error)))}
    # A killed child may never reach its own exception handler.
    for name in ('stdout', 'stderr'):
        if getattr(error, name, None):
            detail[name] = redact(getattr(error, name))
    return detail


def clean_facts(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'(?i)token|password|secret|cookie|authorization|api_key', k)
                else clean_facts(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_facts(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def spool(record):
    """Local fallback is evidence of a persistence failure, not durable success."""
    path = Path('risk_audit_spool.jsonl')
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as stream:
        stream.write(json.dumps(record, ensure_ascii=False)+'\n')


class Audit:
    def __init__(self, queue, operation, job_id=''):
        if not JOB_ID.fullmatch(operation) or job_id and not JOB_ID.fullmatch(job_id):
            raise ValueError('감사 실행 식별자 오류')
        self.queue, self.saved = queue, None
        attempt = uuid.uuid4().hex
        self.path = 'audit/'+(job_id or operation)+'/'+attempt+'.json'
        self.value = {'schema_version': 1, 'operation': operation, 'job_id': job_id,
                      'attempt_id': attempt, 'started_at': now().isoformat(), 'stage': 'started',
                      'state': 'running', 'events': [], 'github_run_id': os.environ.get('GITHUB_RUN_ID', ''),
                      'github_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT', '')}

    def event(self, stage=None, *, state='running', code='', error=None, facts=None, required=True, advance=True):
        stage = stage or self.value['stage']
        if stage not in STAGES or state not in {'running', 'completed', 'failed'}:
            raise ValueError('감사 단계·상태 오류')
        event = {'at': now().isoformat(), 'stage': stage, 'state': state, 'code': code}
        if error is not None:
            event['error'] = error_detail(error)
        if facts:
            # Facts are observations only. No locals, credentials, or request headers.
            event['facts'] = clean_facts(facts)
        self.value.update(state=state, updated_at=event['at'])
        if advance:
            self.value['stage'] = stage
        self.value['events'].append(event)
        try:
            previous, sha = getattr(self.queue, 'get_fresh', self.queue.get)(self.path)
            if previous != self.saved:
                raise RuntimeError('감사 기록 변경 — 덮어쓰기 금지')
            self.queue.put(self.path, self.value, sha)
            self.saved = copy.deepcopy(self.value)
            return True
        except Exception as failure:
            fallback = {'audit': self.value, 'persistence_error': error_detail(failure)}
            try:
                spool(fallback)
            except Exception:
                pass  # Report unknown persistence, never claim durable storage.
            print('RISK_AUDIT state=failed recorded=false stage='+stage)
            if required:
                raise RuntimeError('비공개 감사 기록 저장 실패 — 새 발송 단계 진행 금지') from failure
            return False


def checkpoint(stage, **facts):
    if audit := ACTIVE.get():
        audit.event(stage, facts=facts or None)


def record_error(stage, error, code='runtime_error', **facts):
    if audit := ACTIVE.get():
        return audit.event(stage, code=code, error=error, facts=facts, required=False, advance=False)
    return False


def record_errors(stage, errors):
    """Persist every diagnostic in one event instead of one Git commit per error."""
    if not errors or not (audit := ACTIVE.get()):
        return False
    details = [{'code': entry['code'], 'error': error_detail(entry['error']),
                'facts': entry.get('facts', {})} for entry in errors]
    return audit.event(stage, code='runtime_errors',
                       facts={'error_count': len(details), 'errors': details},
                       required=False, advance=False)


def run_audited(queue, operation, job_id, action):
    audit = Audit(queue, operation, job_id)
    token = ACTIVE.set(audit)
    private_verified = False
    try:
        queue.preflight()  # No diagnostic payload may enter a public repository.
        private_verified = True
        audit.event()
        result = action()
        audit.event(state='failed' if audit.value['state'] == 'failed' else 'completed')
        return result
    except Exception as error:
        if private_verified:
            facts = {}
            if job_id:
                try:
                    job, _ = getattr(queue, 'get_fresh', queue.get)('jobs/'+job_id+'.json')
                    if job:
                        facts = {k: job.get(k) for k in ('status', 'attempted_count', 'history_saved', 'ack')}
                        facts['accepted_receipts'] = sum(v == 'accepted' for v in job.get('receipts', {}).values())
                except Exception as snapshot_error:
                    facts['delivery_snapshot_error'] = error_detail(snapshot_error)
            saved = audit.event(state='failed', code='operation_failed', error=error, facts=facts, required=False)
        else:
            saved = False
            try:
                spool({'operation': operation, 'job_id': job_id, 'stage': 'started', 'error': error_detail(error)})
            except Exception:
                pass
        print('RISK_AUDIT state=failed recorded='+str(saved).lower()+' stage='+audit.value['stage'])
        error._risk_audited = True
        raise
    finally:
        ACTIVE.reset(token)


def persist_unhandled(error, stage='config', operation='bootstrap'):
    """Config/credential/bootstrap failures occur before an audit can start."""
    if getattr(error, '_risk_audited', False):
        return
    try:
        queue = Queue()
        queue.preflight()
        Audit(queue, operation).event(stage, state='failed', code='unhandled_failure', error=error)
    except Exception as failure:
        try:
            spool({'stage': stage, 'error': error_detail(error), 'persistence_error': error_detail(failure)})
        except Exception:
            pass
        print('RISK_AUDIT state=failed recorded=false stage='+stage)


def observe_pending(queue, job):
    """Record silence as missing evidence, never invent a platform error cause."""
    from datetime import datetime, timedelta, timezone
    if job.get('status') != 'pending' or job.get('mode') != 'production':
        return
    jid = job['job_id']
    if queue.get('results/'+jid+'.json')[0] is not None:
        return
    attempt, _ = queue.get('analysis-attempts/'+jid+'.json')
    updated = (attempt or {}).get('updated_at', job['created_at'])
    state = (attempt or {}).get('state')
    code = 'analysis_'+state if state in {'blocked', 'failed'} else (
           'analysis_stalled' if attempt else 'analysis_evidence_missing')
    try:
        try:
            instant = datetime.fromisoformat(updated.replace('Z', '+00:00'))
        except ValueError:
            # Preserve compatibility with an observed Work UTC timestamp format.
            instant = datetime.strptime(updated, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc)
        if instant.tzinfo is None or instant > now():
            raise ValueError('진행 기록 시각·시간대 오류')
    except (ValueError, TypeError, AttributeError):
        code = 'analysis_attempt_invalid'
    else:
        if state not in {'blocked', 'failed'} and now()-instant < timedelta(minutes=60):
            return
    suffix = 'analysis-silent' if code in {'analysis_stalled', 'analysis_evidence_missing'} else code.replace('_', '-')
    path = 'incidents/'+jid+'-'+suffix+'.json'
    if queue.get(path)[0] is not None:
        return
    queue.put(path, {'schema_version': 1, 'job_id': jid, 'observed_at': now().isoformat(),
                    'code': code,
                    'cause_confirmed': False, 'last_updated_at': updated,
                    'last_stage': (attempt or {}).get('stage', 'unknown'),
                    'processed_count': (attempt or {}).get('processed_count'),
                    'input_count': job.get('input_count'), 'result_present': False,
                    'receipts_count': len(job.get('receipts', {}))})


def workflow_main():
    # Only fixed Actions outcomes; no shell logs or response bodies on stdout.
    operation = os.environ['RISK_AUDIT_OPERATION']
    jid = os.environ.get('RISK_AUDIT_JOB_ID', '')
    if not jid:
        jid = json.loads(Path('risk_subscription_request.json').read_text())['job_id']
    outcomes = {k: os.environ.get('RISK_AUDIT_'+k.upper(), '') for k in ('consume', 'history', 'ack')}
    def record():
        details = dict(outcomes)
        history_log = Path('risk_history_error.log')
        if history_log.exists():
            details['history_log'] = history_log.read_text(errors='replace')
        ACTIVE.get().event('workflow_finalize', state='failed' if 'failure' in outcomes.values() else 'completed',
                           code='workflow_outcomes', facts=details)
    run_audited(Queue(), operation, jid, record)


if __name__ == '__main__':
    workflow_main()
