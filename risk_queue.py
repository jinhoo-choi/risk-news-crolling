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


class QueueConflict(RuntimeError):
    """A confirmed HTTP 409; not an ambiguous network/write failure."""

    def __init__(self, request_id='unconfirmed', kind='conflict', method='PUT', code=409):
        self.request_id, self.kind = request_id, kind
        super().__init__('비공개 큐 '+method+' HTTP '+str(code)+'; kind='+kind+'; request_id='+request_id)


class QueueReadUnavailable(RuntimeError):
    """Only a transient GET failure; never evidence to retry a write."""

    def __init__(self, message, code=0, request_id='unconfirmed'):
        self.code, self.request_id = code, request_id
        super().__init__(message)


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
                'Cache-Control': 'no-cache',
                'X-GitHub-Api-Version': '2022-11-28'})
        attempts = 3 if method == 'GET' else 1
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                request_id = error.headers.get('X-GitHub-Request-Id', '') if error.headers else ''
                if not re.fullmatch(r'[A-Za-z0-9:-]{1,100}', request_id):
                    request_id = 'unconfirmed'
                if (error.code == 409 and method == 'PUT'
                        or error.code in {409, 422} and method == 'PATCH' and suffix == '/git/refs/heads/main'):
                    # Never expose GitHub's raw response (it may contain private data).
                    try:
                        message = json.loads(error.read(8192)).get('message', '').lower()
                        kind = 'sha_conflict' if 'sha' in message else 'ref_conflict' if 'reference' in message else 'conflict'
                    except (ValueError, AttributeError, TypeError):
                        kind = 'conflict'
                    raise QueueConflict(request_id, kind, method, error.code) from None
                if error.code == 404 and method == 'GET':
                    return None
                if error.code in {429, 500, 502, 503, 504} and attempt+1 < attempts:
                    time.sleep(attempt+1)
                    continue
                message = f'비공개 큐 {method} HTTP {error.code}; request_id={request_id}'
                if method == 'GET' and error.code in {429, 500, 502, 503, 504}:
                    raise QueueReadUnavailable(message, error.code, request_id) from None
                raise RuntimeError(message) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt+1 < attempts:
                    time.sleep(attempt+1)
                    continue
                if method == 'GET':
                    raise QueueReadUnavailable('비공개 큐 GET 네트워크 실패') from None
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

    def get(self, path, ref='main'):
        if '..' in path or path.startswith('/'):
            raise ValueError('큐 경로 오류')
        obj = self.request('/contents/' + PREFIX + '/' + urllib.parse.quote(path, safe='/') + '?ref='+urllib.parse.quote(ref, safe=''))
        if obj is None:
            return None, None
        if obj.get('type') != 'file':
            raise RuntimeError('큐 자료 형식 오류')
        if obj.get('encoding') == 'base64' and obj.get('content'):
            raw = base64.b64decode(obj['content'])
        else:
            # Contents API omits content for files over 1 MiB. Git blobs retain it.
            blob = self.request('/git/blobs/'+obj['sha'])
            if not blob or blob.get('encoding') != 'base64':
                raise RuntimeError('대용량 큐 blob 읽기 실패')
            raw = base64.b64decode(blob['content'])
        actual = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
        if actual != obj['sha']:
            raise RuntimeError('비공개 큐 blob 해시 불일치')
        return strict_json(raw.decode()), obj['sha']

    def get_fresh(self, path):
        # Pin a commit: a cached Contents response for the moving main ref must
        # never erase a receipt written by the preceding SMTP recipient.
        head = self.request('/git/ref/heads/main')['object']['sha']
        return self.get(path, ref=head)

    def put(self, path, value, sha=None):
        if '..' in path or path.startswith('/'):
            raise ValueError('큐 경로 오류')
        body = {'branch': 'main', 'message': 'chore: risk subscription state [skip ci]',
                'content': base64.b64encode(json.dumps(value, ensure_ascii=False, indent=2).encode()).decode()}
        if sha:
            body['sha'] = sha
        for attempt in range(3):
            try:
                return self.request('/contents/' + PREFIX + '/' + urllib.parse.quote(path, safe='/'), 'PUT', body)
            except QueueConflict:
                current, current_sha = self.get_fresh(path)
                if current_sha and current == value:
                    return {'content': {'sha': current_sha}, 'confirmed_existing': True}
                # Same file SHA, different branch head: retry the *same* CAS.
                # A changed file/receipt is a conflict, never overwrite/merge it.
                if current_sha != sha or attempt == 2:
                    raise
                time.sleep(attempt+1)

    def list_input_entries(self, job_id=None):
        """List live input trees, including jobs no longer present in index.json."""
        if job_id is not None and not JOB_ID.fullmatch(job_id):
            raise ValueError('큐 회차 ID 오류')
        head = self.request('/git/ref/heads/main')['object']['sha']
        tree_sha = self.request('/git/commits/'+head)['tree']['sha']
        parts = (PREFIX+'/inputs').split('/') + ([job_id] if job_id else [])
        for part in parts:
            tree = self.request('/git/trees/'+tree_sha)
            if tree.get('truncated'):
                raise RuntimeError('큐 입력 트리 목록 불완전 — 정리 보류')
            entry = next((e for e in tree['tree'] if e['path'] == part), None)
            if entry is None:
                return []
            if entry['type'] != 'tree':
                raise RuntimeError('큐 입력 디렉터리 형식 오류')
            tree_sha = entry['sha']
        tree = self.request('/git/trees/'+tree_sha)
        if tree.get('truncated'):
            raise RuntimeError('큐 입력 트리 목록 불완전 — 정리 보류')
        return tree['tree']

    def put_many(self, values, expected, deletions=()):
        """Publish atomically; rebase only confirmed ref races with intact leases."""
        deletions = list(deletions)
        for path in deletions:
            parts = path.split('/')
            if (len(parts) != 3 or parts[0] != 'inputs' or not JOB_ID.fullmatch(parts[1])
                    or not JOB_ID.fullmatch(parts[2].removesuffix('.json'))
                    or not parts[2].endswith('.json') or not expected.get(path)
                    or path in values):
                raise ValueError('큐 입력 삭제 경로·SHA 오류')
        entries = []
        for path, value in values.items():
            if '..' in path or path.startswith('/'):
                raise ValueError('큐 분할 경로 오류')
            entries.append({'path': PREFIX+'/'+path, 'mode': '100644', 'type': 'blob',
                            'content': json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)})
        entries.extend({'path': PREFIX+'/'+path, 'mode': '100644', 'type': 'blob', 'sha': None}
                       for path in deletions)
        for attempt in range(3):
            head = self.request('/git/ref/heads/main')['object']['sha']
            commit = self.request('/git/commits/'+head)
            for path, wanted in expected.items():
                if self.get(path, ref=head)[1] != wanted:
                    raise RuntimeError('큐 원본 SHA 변경 — 일괄 쓰기 보류')
            tree = self.request('/git/trees', 'POST', {'base_tree': commit['tree']['sha'], 'tree': entries})
            new = self.request('/git/commits', 'POST', {'message':'chore: private risk queue shards [skip ci]',
                              'tree':tree['sha'], 'parents':[head]})
            try:
                self.request('/git/refs/heads/main', 'PATCH', {'sha':new['sha'], 'force':False})
                return new['sha']
            except QueueConflict:
                current = self.request('/git/ref/heads/main')['object']['sha']
                if current == new['sha']:
                    return new['sha']
                if current == head or attempt == 2:
                    raise
                time.sleep(attempt+1)


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
