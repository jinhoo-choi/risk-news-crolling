"""Optional private delivery artifacts, persisted after history/ACK in Actions."""
import json
import os
from pathlib import Path
from risk_queue import JOB_ID
from risk_audit import record_error, spool

STAGED = Path('risk_diagnostic_artifacts.json')


def persist(queue, jid, values):
    if not JOB_ID.fullmatch(jid) or any(p not in {
            d+'/'+jid+'.json' for d in ('metrics','filter-logs','previews','logs')} for p in values):
        raise ValueError('진단 자료 경로 오류')
    try:
        changed, leases = {}, {}
        for path, value in values.items():
            old, sha = getattr(queue, 'get_fresh', queue.get)(path)
            if old == value:
                continue
            if old is not None:
                raise ValueError('기존 진단 자료 충돌 — 원본 보존')
            changed[path], leases[path] = value, sha
        if changed:
            queue.put_many(changed, leases)
        return True
    except Exception as error:
        try:spool({'operation':'delivery_diagnostics','job_id':jid,'artifacts':values,'error_type':type(error).__name__})
        except Exception:pass
        record_error('cleanup', error, 'diagnostic_artifacts_unavailable')
        print('RISK_DIAGNOSTICS recorded=false')
        return False


def finish(queue, jid, values):
    if not values:
        return
    if os.environ.get('RISK_DEFER_DIAGNOSTICS') == 'true':
        # A private runner file; never publish article content in public logs.
        try:
            with STAGED.open('x') as stream:
                os.chmod(STAGED,0o600)
                json.dump({'job_id':jid,'values':values},stream,ensure_ascii=False)
        except Exception as error:
            record_error('cleanup',error,'diagnostic_stage_failed')
            print('RISK_DIAGNOSTICS recorded=false')
    else:
        persist(queue,jid,values)


if __name__=='__main__':
    if STAGED.exists():
        try:
            from risk_queue import Queue
            from risk_analysis_guard import strict_json
            document=strict_json(STAGED.read_text())
            queue=Queue()
            queue.preflight()
            saved=persist(queue,document['job_id'],document['values'])
            print('RISK_DIAGNOSTICS recorded='+str(saved).lower())
        except Exception:
            # This step cannot fail or alter a completed delivery.
            print('RISK_DIAGNOSTICS recorded=false')
