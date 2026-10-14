"""리스크봇 검수·운영 비공개 큐. 원문·분석·주소·쿠키의 공개 저장을 금지한다."""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from risk_analysis_guard import strict_json

REPOSITORY = 'jinhoo-choi/insight-bot-data'
PREFIX = 'subscription/risk'
BOT_ID = 'risk_monitor'
POLICY = 'risk-subscription-v1'
JOB_ID = re.compile(r'^[A-Za-z0-9_-]{1,80}$')


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class Queue:
    def __init__(self):
        self.token = os.environ.get('RISK_QUEUE_TOKEN', '')
        if not self.token:
            raise RuntimeError('RISK_QUEUE_TOKEN 미설정 — 비공개 자료 저장 중단')

    def request(self, suffix, method='GET', data=None, repository=REPOSITORY):
        if repository not in {REPOSITORY, 'jinhoo-choi/risk-news-crolling'}:
            raise ValueError('허용된 큐·운영 저장소만 접근')
        request = urllib.request.Request('https://api.github.com/repos/' + repository + suffix,
            data=json.dumps(data).encode() if data is not None else None,
            method=method, headers={'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28'})
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 404 and method == 'GET':
                    return None
                if error.code in {429, 500, 502, 503, 504} and attempt+1 < attempts:
                    time.sleep(attempt+1)
                    continue
                raise RuntimeError(f'비공개 큐 {method} HTTP {error.code}') from None
            except urllib.error.URLError:
                if attempt+1 < attempts:
                    time.sleep(attempt+1)
                    continue
                raise RuntimeError('비공개 큐 네트워크 실패 — 쓰기 자동 재시도 없음') from None

    def preflight(self):
        repository = self.request('')
        if not repository or not repository.get('private'):
            raise RuntimeError('비공개 검수 저장소 접근 실패')
        if not self.request('/git/ref/heads/main'):
            raise RuntimeError('비공개 검수 main 브랜치 없음')

    def source(self, path):
        if path not in {'risk_subscription_config.json', 'seen_news.json', 'exposure_data.csv'}:
            raise ValueError('운영 설정·이력 읽기만 허용')
        obj = self.request('/contents/'+path+'?ref=main', repository='jinhoo-choi/risk-news-crolling')
        if not obj or obj.get('type') != 'file':
            raise RuntimeError('운영 설정·이력 읽기 실패')
        return base64.b64decode(obj['content']), obj['sha']

    def get(self, path):
        if '..' in path or path.startswith('/'):
            raise ValueError('큐 경로 오류')
        obj = self.request('/contents/' + PREFIX + '/' + urllib.parse.quote(path, safe='/') + '?ref=main')
        if obj is None:
            return None, None
        if obj.get('type') != 'file':
            raise RuntimeError('큐 자료 형식 오류')
        return strict_json(base64.b64decode(obj['content']).decode()), obj['sha']

    def put(self, path, value, sha=None):
        if '..' in path or path.startswith('/'):
            raise ValueError('큐 경로 오류')
        body = {'branch': 'main', 'message': 'chore: risk subscription state [skip ci]',
                'content': base64.b64encode(json.dumps(value, ensure_ascii=False, indent=2).encode()).decode()}
        if sha:
            body['sha'] = sha
        return self.request('/contents/' + PREFIX + '/' + urllib.parse.quote(path, safe='/'), 'PUT', body)


def connection(queue):
    queue.preflight()
    run_id = os.environ.get('GITHUB_RUN_ID', now().strftime('%Y%m%dT%H%M%S'))
    if not JOB_ID.fullmatch(run_id):
        raise ValueError('검수 회차 ID 오류')
    path = 'checks/' + run_id + '.json'
    probe = {'bot_id': BOT_ID, 'checked_at': now().isoformat(), 'read_write': True,
             'paid_ai_calls': 0, 'smtp_calls': 0}
    queue.put(path, probe)
    if queue.get(path)[0] != probe:
        raise RuntimeError('비공개 검수 쓰기 후 읽기 불일치')
    print('리스크봇 비공개 검수 연결 완료; API0/SMTP0')
