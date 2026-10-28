"""Regression tests for real-search control flow. Provider replies are mocked, not real internet QA."""
from app.adapters import PublicItem


def test_direct_search_ignores_preset_keywords(context, monkeypatch):
    main, client = context
    monkeypatch.setattr(main, 'fetch_hackernews', lambda q: [PublicItem('Hacker News','装修询价：找施工团队', '最近店面要装修，请问报价', 'https://news.ycombinator.com/item?id=9998')])
    resp = client.post('/api/search-online',json={'query':'装修','sources':['hackernews']})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['query'] == '装修'
    assert data['count'] == 1
    assert data['reports'][0]['inserted'] == 1
    assert data['items'][0]['source'] == 'Hacker News'
    assert data['items'][0]['matched_terms'] == ['装修']
    assert client.get('/api/items?source=Hacker%20News').json()[0]['title'].startswith('装修')
    again=client.post('/api/search-online',json={'query':'装修','sources':['hackernews']}).json()
    assert again['count'] == 1 and again['reports'][0]['duplicates'] == 1
    assert again['reports'][0]['inserted'] == 0


def test_search_partial_failure_reports_exact_source(context, monkeypatch):
    main, client = context
    monkeypatch.setattr(main, 'fetch_google_news', lambda q: [PublicItem('Google News','店面装修新闻', '装修趋势报告', 'https://news.google.com/articles/123')])
    def fail(q):
        raise RuntimeError('HTTP 403 rate limited')
    monkeypatch.setattr(main, 'fetch_github', fail)
    out=client.post('/api/search-online',json={'query':'店面装修','sources':['googlenews','github']}).json()
    assert out['count'] == 1
    assert out['reports'][0]['inserted'] == 1
    assert 'HTTP 403' in out['reports'][1]['error']
    assert out['items'][0]['source'] == 'Google News'
    assert out['items'][0]['score'] == 0
    assert any('HTTP 403' in x['error'] for x in client.get('/api/ingest-logs').json())


def test_search_validates_sources_query(context):
    main, client = context
    assert client.post('/api/search-online',json={'query':'word','sources':['demo']}).status_code == 422
    assert client.post('/api/search-online',json={'query':'  ','sources':['github']}).status_code == 422
    assert client.post('/api/search-online',json={'query':'q','sources':[]}).status_code == 422
    health=client.get('/api/health').json()
    assert health['online_search'] and health["version"] == "4.1.2"
    page=client.get('/').text
    assert 'id="apiPlatforms"' in page and '公开网站' in page


def test_zero_result_is_not_mislabeled_as_success_lead(context,monkeypatch):
    main, client=context
    monkeypatch.setattr(main,'fetch_hackernews',lambda q:[])
    out=client.post('/api/search-online',json={'query':'不存在','sources':['hackernews']}).json()
    assert out['count']==0
    assert out['reports'][0]['fetched']==0 and not out['reports'][0]['error']


def test_news_rss_sources(monkeypatch):
    from app import adapters
    from urllib.parse import urlparse,parse_qs
    def responder(url, **kwargs):
        assert ('news.google.com' in url or 'www.bing.com' in url)
        params=parse_qs(urlparse(url).query)
        assert params.get('q') == ['AI 客服']
        class Response:
            content = b'<rss version="2.0"><channel><item><title>AI News</title><description>Public source</description><link>https://example.org/story</link></item></channel></rss>'
        return Response()
    monkeypatch.setattr(adapters,'_get',responder)
    for fn in (adapters.fetch_google_news, adapters.fetch_bing_news):
        result=fn('AI 客服')
        assert result[0].source in ('Google News','Bing News')
        assert result[0].title=='AI News' and result[0].url=='https://example.org/story'
