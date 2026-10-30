"""Send a recent, previously completed production report to the existing owner only.

The stored subject and HTML remain unchanged. This is explicitly a presentation
replay, not a new analysis or a retry of the production job. All replay receipts
are private and independent of the production queue and delivery history.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import smtplib
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from risk_queue import JOB_ID, Queue, digest, now
from risk_result_validator import utc_time
from risk_owner_verification import OWNER, OwnerSMTP, snapshot

FIELDS = {'request_id', 'source_job_id', 'packet_hash', 'expires_at', 'confirm_owner_replay'}


def validate_request(request):
    if (not isinstance(request, dict) or set(request) != FIELDS
            or request['confirm_owner_replay'] is not True
            or not JOB_ID.fullmatch(request['request_id'])
            or not re.fullmatch(r'risk-production-[A-Za-z0-9_-]+', request['source_job_id'])
            or not re.fullmatch(r'[0-9a-f]{64}', request['packet_hash'])):
        raise RuntimeError('Invalid owner presentation request')
    remaining = utc_time(request['expires_at']) - now()
    if not timedelta(0) < remaining <= timedelta(hours=24):
        raise RuntimeError('Presentation request expired or too far in the future')


def validate_source(request, source, packet):
    if (not source or source.get('mode') != 'production' or source.get('status') != 'completed'
            or source.get('ack') is not True or source.get('history_saved') is not True
            or source.get('job_id') != request['source_job_id'] or not source.get('receipts')
            or set(source['receipts']) != set(source.get('expected_receipts', []))
            or any(v != 'accepted' for v in source['receipts'].values())):
        raise RuntimeError('Source production report is not completed')
    if (not isinstance(packet, dict) or packet.get('packet_hash') != request['packet_hash']
            or packet.get('packet_hash') != source.get('outbox_hash')
            or packet['packet_hash'] != digest({k:v for k,v in packet.items() if k != 'packet_hash'})
            or any(packet.get(k) != source.get(k) for k in ('job_id','input_hash','policy_hash','body_manifest_hash'))
            or packet.get('self_test') is not False or packet.get('paid_ai_calls') != 0
            or not isinstance(packet.get('html'), str) or not packet['html']
            or not isinstance(packet.get('subject'), str) or '[리스크 탐지]' not in packet['subject']
            or '\n' in packet['subject'] or '\r' in packet['subject']):
        raise RuntimeError('Stored production report hash or type mismatch')
    age = now() - utc_time(packet['created_at'])
    if not timedelta(0) <= age <= timedelta(hours=72):
        raise RuntimeError('Use a recent report prepared within 72 hours')


def run(request, queue, bot, smtp_factory=smtplib.SMTP_SSL):
    validate_request(request)
    path = 'presentation/'+request['request_id']+'.json'
    prior, _ = queue.get_fresh(path)
    if prior is not None:
        if prior.get('request_hash') != digest(request):
            raise RuntimeError('Presentation request ID was reused with changed contents')
        if prior.get('status') == 'verified':
            return {'status':'already_sent','smtp_calls':0,'request_id':request['request_id']}
        raise RuntimeError('Existing presentation attempt; inspect before any new send')
    source_path = 'jobs/'+request['source_job_id']+'.json'
    packet_path = 'outbox/'+request['source_job_id']+'.json'
    source, _ = queue.get_fresh(source_path)
    packet, _ = queue.get_fresh(packet_path)
    validate_source(request, source, packet)
    source_hash = digest(source)
    before = snapshot()
    html_hash = hashlib.sha256(packet['html'].encode()).hexdigest()
    replay = {'schema_version':1,'mode':'verification','test_kind':'presentation_replay',
              'job_id':request['request_id'],'request_hash':digest(request),'status':'pending',
              'source_job_id':request['source_job_id'],'source_hash':source_hash,
              'source_packet_hash':packet['packet_hash'],'html_sha256':html_hash,
              'source_report_created_at':packet['created_at'],
              'created_at':now().isoformat(),'expires_at':request['expires_at'],
              'self_test':True,'receipts':{},'attempted_count':0,'paid_ai_calls':0}
    queue.put(path, replay)
    stored, _ = queue.get_fresh(path)
    if stored != replay:
        raise RuntimeError('Presentation reservation readback mismatch')
    bot.EMAIL_RECEIVERS, bot.EMAIL_CC = [], []
    bot.NO_RESULT_RECEIVER = OWNER
    guard = OwnerSMTP(smtp_factory, queue, path)
    def deny_post(*args, **kwargs):
        raise RuntimeError('No inference or HTTP POST in presentation replay')
    with patch.object(bot.smtplib, 'SMTP_SSL', guard.factory), patch.object(bot.requests, 'post', side_effect=deny_post), contextlib.redirect_stdout(io.StringIO()):
        bot.send_email(packet['subject'], packet['html'], self_only=True)
    current, sha = queue.get_fresh(path)
    unchanged, _ = queue.get_fresh(source_path)
    if (guard.accepted != 1 or len(current['receipts']) != 1
            or any(v != 'accepted' for v in current['receipts'].values())
            or before != snapshot() or digest(unchanged) != source_hash):
        raise RuntimeError('Presentation receipt or production isolation mismatch')
    current.update(status='verified',verified_at=now().isoformat(),
                   production_history_writes=0,production_source_unchanged=True,
                   before_hashes=before,after_hashes=snapshot())
    queue.put(path,current,sha)
    final, _ = queue.get_fresh(path)
    if final != current:
        raise RuntimeError('Presentation completion readback mismatch; do not resend')
    return {'status':'verified','request_id':request['request_id'],'smtp_accepted':1,
            'smtp_calls':guard.attempts,'html_sha256':html_hash,'paid_ai_calls':0,
            'production_history_writes':0}


def main():
    if os.environ.get('GITHUB_RUN_ATTEMPT','1') != '1':
        raise RuntimeError('Do not rerun presentation delivery attempts')
    if os.environ.get('EMAIL_RECEIVER') or os.environ.get('EMAIL_CC'):
        raise RuntimeError('Group recipients must not be supplied')
    request = json.loads(Path('risk_presentation_request.json').read_text())
    validate_request(request)
    queue=Queue()
    queue.preflight()
    os.environ.update(EMAIL_RECEIVER='',EMAIL_CC='',NO_RESULT_RECEIVER=OWNER,
                      ANTHROPIC_API_KEY='subscription-api-disabled',RISK_ENGINE='subscription',
                      NAVER_CLIENT_ID='presentation-not-used',NAVER_CLIENT_SECRET='presentation-not-used')
    with contextlib.redirect_stdout(io.StringIO()):
        import naver_news_monitor as bot
    print(json.dumps(run(request,queue,bot)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No article content, addresses, credentials, or private exception payloads in public logs.
        print('Presentation delivery stopped:',type(error).__name__)
        raise SystemExit(1)
