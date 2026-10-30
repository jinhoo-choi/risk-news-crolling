"""Offline checks of native owner delivery, exact HTML, and replay isolation."""
import copy
import contextlib
import hashlib
import io
import os
import smtplib
import unittest
from datetime import timedelta
from email import message_from_string
from email.header import decode_header, make_header
from email.utils import getaddresses
from unittest.mock import patch

os.environ.setdefault('EMAIL_SENDER','sender@example.invalid')
os.environ.setdefault('EMAIL_PASSWORD','offline-only')
os.environ.setdefault('EMAIL_RECEIVER','')
os.environ.setdefault('ANTHROPIC_API_KEY','subscription-api-disabled')
os.environ.setdefault('NAVER_CLIENT_ID','offline-only')
os.environ.setdefault('NAVER_CLIENT_SECRET','offline-only')
with contextlib.redirect_stdout(io.StringIO()):
    import naver_news_monitor as bot
import risk_presentation_send as app
from risk_owner_verification import OWNER
from risk_queue import digest, now


class Queue:
    def __init__(self, values): self.values=copy.deepcopy(values); self.writes=[]
    def get_fresh(self,path):
        value=self.values.get(path)
        return copy.deepcopy(value), digest(value) if value else None
    get=get_fresh
    def put(self,path,value,sha=None):
        current=self.values.get(path)
        if sha != (digest(current) if current else None): raise RuntimeError('CAS conflict')
        self.values[path]=copy.deepcopy(value); self.writes.append(path)


class SMTP:
    messages=[]
    fail=False
    def __init__(self,host,port,**kwargs): assert (host,port)==('smtp.gmail.com',465)
    def ehlo(self): pass
    def login(self,*args): pass
    def quit(self): pass
    def close(self): pass
    def sendmail(self,sender,targets,raw):
        self.messages.append((targets,raw))
        if self.fail: raise smtplib.SMTPServerDisconnected('offline uncertain DATA')
        return {}


class Tests(unittest.TestCase):
    def setUp(self):
        self.source={'job_id':'risk-production-fixture','mode':'production','status':'completed',
                     'ack':True,'history_saved':True,'receipts':{'prior':'accepted'},
                     'expected_receipts':['prior'],'input_hash':'i','policy_hash':'p','body_manifest_hash':'b'}
        self.packet={'job_id':self.source['job_id'],'input_hash':'i','policy_hash':'p','body_manifest_hash':'b',
                     'self_test':False,'paid_ai_calls':0,'subject':'❗ [리스크 탐지] 이전 기준시각',
                     'html':'<html><body>실제 보고서 형식 보존 검사</body></html>',
                     'created_at':(now()-timedelta(hours=12)).isoformat()}
        self.packet['packet_hash']=digest(self.packet)
        self.source['outbox_hash']=self.packet['packet_hash']
        self.request={'request_id':'presentation-fixture','source_job_id':self.source['job_id'],
                      'packet_hash':self.packet['packet_hash'],'expires_at':(now()+timedelta(hours=1)).isoformat(),
                      'confirm_owner_replay':True}
        self.queue=Queue({'jobs/'+self.source['job_id']+'.json':self.source,
                          'outbox/'+self.source['job_id']+'.json':self.packet})
        SMTP.messages=[];SMTP.fail=False
    def run_send(self):
        return app.run(self.request,self.queue,bot,SMTP)
    def test_exact_native_body_and_owner_only(self):
        before=copy.deepcopy(self.queue.values)
        result=self.run_send()
        self.assertEqual(result['smtp_accepted'],1)
        targets,raw=SMTP.messages[0];msg=message_from_string(raw)
        self.assertEqual(targets,[OWNER]);self.assertEqual(getaddresses(msg.get_all('To'))[0][1],OWNER)
        self.assertIsNone(msg['Cc']);self.assertIsNone(msg['Bcc'])
        self.assertEqual(str(make_header(decode_header(msg['Subject']))),bot._sanitize_header_text(self.packet['subject']))
        body=[p.get_payload(decode=True).decode('utf-8') for p in msg.walk() if p.get_content_type()=='text/html']
        self.assertEqual(body,[self.packet['html']]);self.assertTrue(msg['Date']);self.assertTrue(msg['Message-ID'])
        for path,value in before.items(): self.assertEqual(self.queue.values[path],value)
        self.assertTrue(all(p.startswith('presentation/') for p in self.queue.writes))
    def test_same_request_does_not_send_twice(self):
        self.run_send();self.assertEqual(self.run_send()['smtp_calls'],0);self.assertEqual(len(SMTP.messages),1)
    def test_changed_existing_request_rejected(self):
        self.run_send();self.request['expires_at']=(now()+timedelta(hours=2)).isoformat()
        with self.assertRaises(RuntimeError): self.run_send()
        self.assertEqual(len(SMTP.messages),1)
    def test_uncertain_smtp_is_never_retried(self):
        SMTP.fail=True
        with self.assertRaises(RuntimeError):self.run_send()
        self.assertEqual(len(SMTP.messages),1)
        with self.assertRaises(RuntimeError):self.run_send()
        self.assertEqual(len(SMTP.messages),1)
    def test_tampered_body_rejected(self):
        self.queue.values['outbox/'+self.source['job_id']+'.json']['html']+='changed'
        with self.assertRaises(RuntimeError):self.run_send()
        self.assertEqual(SMTP.messages,[])
    def test_incomplete_source_rejected(self):
        self.queue.values['jobs/'+self.source['job_id']+'.json']['ack']=False
        with self.assertRaises(RuntimeError):self.run_send()
        self.assertEqual(SMTP.messages,[])
    def test_expired_request_rejected(self):
        self.request['expires_at']=(now()-timedelta(seconds=1)).isoformat()
        with self.assertRaises(RuntimeError):self.run_send()
    def test_old_source_rejected(self):
        self.packet['created_at']=(now()-timedelta(days=4)).isoformat()
        self.packet['packet_hash']=digest({k:v for k,v in self.packet.items() if k!='packet_hash'})
        self.request['packet_hash']=self.packet['packet_hash'];self.source['outbox_hash']=self.packet['packet_hash']
        with self.assertRaises(RuntimeError):app.validate_source(self.request,self.source,self.packet)
    def test_confirmation_required(self):
        self.request['confirm_owner_replay']=False
        with self.assertRaises(RuntimeError):self.run_send()
    def test_unexpected_recipient_field_rejected(self):
        self.request['recipient']='stranger@example.invalid'
        with self.assertRaises(RuntimeError):self.run_send()


if __name__=='__main__':unittest.main()
