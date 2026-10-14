"""Validated subscription answers supplied to the existing native pipeline.

Responses use the existing native parsing/post-processing without HTTP inference.
The saved result is the analysis; these are not independent model reviews.
"""
import copy
import hashlib
import json


class Response:
    status_code = 200
    def __init__(self, value):
        self.text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    def raise_for_status(self):
        pass
    def json(self):
        return {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': self.text}]}


class Provider:
    def __init__(self, job, result):
        self.job, self.result = job, result
        self.rows = {r['article_id']: r for r in result['classifications']}
        self.analyses = result['analyses']
        self.bodies = {b['article_id']: b for b in job['body_manifest']}
        self.by_url = {a['url']: a['article_id'] for a in job['input']['items']}
        self.calls = {}

    def analysis(self, article):
        return self.analyses[article['article_id']]

    def attach(self, article):
        a = self.analysis(article)
        b = self.bodies[article['article_id']]
        body = b['body'] if a['body_status'] == 'verified' else ''
        article.update(body=body, _body_failed=a['body_status'] != 'verified',
                       _body_source_url=b.get('source_url', article['url']),
                       _body_source_hash=hashlib.sha256(body.encode()).hexdigest(),
                       _body_risk_confirmed=a['body_status'] == 'verified',
                       _judgment=copy.deepcopy(a['judgment']),
                       related_stocks=copy.deepcopy(a['related_stocks']))

    def response(self, route, article=None, articles=None, offset=0):
        self.calls[route] = self.calls.get(route, 0)+1
        if route == 'classify':
            answer = []
            for i, item in enumerate(articles, offset+1):
                row = self.rows[item['article_id']]
                if row['relevant'] is None:
                    raise RuntimeError('미확인 입력이 있는 회차의 정상 발송 금지')
                value = {'id': i, 'relevant': row['relevant'], 'reason': row['reason']}
                if row['relevant']:
                    a = self.analysis(item)
                    self.attach(item)
                    value.update(grade=a['base_grade'], confidence=a['confidence'],
                                 entity=a['entity'], entities=a['entities'],
                                 event_type=a['event_type'], action=a['action'])
                answer.append(value)
            return Response(answer)
        if route == 'verify_grade':
            return Response([{'id': i, 'grade': self.analysis(a)['base_grade']}
                             for i, a in enumerate(articles, 1)])
        if route == 'verify_body':
            a = self.analysis(article)
            return Response({'risk': True, 'judgment': a['judgment']})
        if route == 'action':
            a = self.analysis(article)
            return Response({'action': a['action'], 'customer_notice': a['customer_notice']})
        if route == 'summary':
            # Native format is a short upper summary. Use only delivered cards.
            labels = list(dict.fromkeys((a.get('entity') or '시장')+' '+a['event_type'] for a in articles))
            return Response(('·'.join(labels))[:40])
        raise RuntimeError('미등록 구독 분석 경로 — API 자동 폴백 금지')

    def install(self, bot):
        bot.RISK_PROVIDER = self
        model = self.result['model_used']
        bot.CLAUDE_MODEL = bot.CLAUDE_FILTER_MODEL = bot.CLAUDE_ACTION_MODEL = model
        bot.CLAUDE_VERIFY_MODEL = model
        bot.CLAUDE_VERIFY_HIGH_MODEL = model
        bot._LAST_ANALYSIS_ENGINE = 'chatgpt_subscription'
        bot._LAST_VERIFY_MODEL = model
        bot.KEYWORDS = ['__subscription_snapshot__']
        bot.get_overseas_keywords = lambda *a, **k: []
        bot.crawl_naver_news = lambda keyword: copy.deepcopy(self.job['input']['raw_articles'])
        def body(url):
            article_id = self.by_url.get(url)
            b = self.bodies.get(article_id, {})
            a = self.analyses.get(article_id, {})
            return b.get('body', '') if a.get('body_status') == 'verified' else ''
        bot.fetch_article_body = body
