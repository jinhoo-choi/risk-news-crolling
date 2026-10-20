"""Validated subscription answers supplied to the existing native pipeline.

Responses use the existing native parsing/post-processing without HTTP inference.
The saved result is the analysis; these are not independent model reviews.
"""
import copy
import hashlib
import json
import re
from datetime import timedelta
from email.utils import parsedate_to_datetime


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
        self.summary_source_ids = []
        self.event_groups = {aid: str(i) for i, group in enumerate(result.get('event_groups', []))
                             for aid in group['article_ids']}
        self.stat_dates = {}
        market_candidates, statistic_members = set(), set()
        source = {a['article_id']: a for a in job['input']['items']}
        for aid, analysis in self.analyses.items():
            body = self.bodies[aid]['body']
            # Successive releases of this one market-wide statistic are one mail
            # item, even if the model split them by reference day or omitted a group.
            # Named-company liquidations, forecasts and other statistics stay apart.
            if (analysis['body_status'] != 'verified' or analysis['entity'] or analysis['entities']
                    or analysis['event_type'] != '반대매매'
                    or analysis['judgment']['확정여부'] != '확정'
                    or not re.search(r'미수금.{0,30}반대매매', analysis['fact_summary'])):
                continue
            market_candidates.add(aid)
            if (not re.search(r'금융투자협회|금투협', body)
                    or not re.search(r'미수금.{0,40}반대매매|반대매매.{0,40}미수금', body, re.S)):
                continue
            quotes = [e['quote'] for e in analysis['evidence']
                      if e['source_segment'] == 'body' and e['quote'] in body]
            if not any(re.search(r'\d[\d,.\s]*(?:억|만)\s*원', q) for q in quotes):
                continue
            statistic_members.add(aid)
            dates = []
            try:
                published = parsedate_to_datetime(source[aid]['pubDate']).date()
                for quote in quotes:
                    for year, month, day in re.findall(
                            r'(?:(20\d{2})년\s*)?(?:(\d{1,2})월\s*)?(?<!\d)(\d{1,2})일', quote):
                        try:
                            date = published.replace(year=int(year or published.year),
                                                     month=int(month or published.month), day=int(day))
                            if date > published and not year:
                                date = (date.replace(year=date.year-1) if month else
                                        (published.replace(day=1)-timedelta(days=1)).replace(day=int(day)))
                            if date <= published:
                                dates.append(date.isoformat())
                        except ValueError:
                            continue
            except (KeyError, TypeError, ValueError, OverflowError):
                pass  # Unknown source date never becomes the publication date.
            self.stat_dates[aid] = max(dates, default='')

        # Coarsen whole reviewed groups; never split one by relabelling only its
        # automatic matches. A mixed company/risk/statistic group cannot bridge
        # into the market series: preserve its reviewed identity instead.
        for group in result.get('event_groups', []):
            members = set(group['article_ids'])
            if members & statistic_members:
                if members <= market_candidates:
                    statistic_members.update(members)
                else:
                    statistic_members.difference_update(members)
        self.event_groups.update(dict.fromkeys(statistic_members, 'market:금투협:미수금반대매매'))

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
                       related_stocks=copy.deepcopy(a['related_stocks']),
                       _subscription_direct_entities=copy.deepcopy(a.get('direct_event_entities', [])))
        article.pop('_subscription_event_group', None)
        article.pop('_subscription_stat_date', None)
        article.pop('_subscription_stat_series', None)
        group = self.event_groups.get(article['article_id'])
        if group is None and self.job.get('event_grouping_version') == 1:
            # Reviewed independent incidents must not enter legacy entity-wide
            # dedup, which would silence a second event at the same company.
            group = 'article:'+article['article_id']
        if group is not None:
            article['_subscription_event_group'] = group
        if group == 'market:금투협:미수금반대매매':
            article['_subscription_stat_series'] = group
            article['fact_summary'] = a['fact_summary']
        if article['article_id'] in self.stat_dates:
            article['_subscription_stat_date'] = self.stat_dates[article['article_id']]

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
            self.summary_source_ids = []
            labels = {}
            for a in articles:
                label = (a.get('entity') or '시장')+' '+self.analysis(a)['judgment']['핵심사건']
                labels.setdefault(label, a['article_id'])
            text = ''
            for label, article_id in labels.items():
                proposed = text+('·' if text else '')+label
                if len(proposed) > 40:
                    break
                text = proposed
                self.summary_source_ids.append(article_id)
            return Response(text)
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
