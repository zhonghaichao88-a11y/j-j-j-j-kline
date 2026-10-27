"""Endpoints behind the 3.3 redesigned dashboard (browser parts mocked)."""
import time
from app.adapters import PublicItem


def _wait(client, jid):
    for _ in range(100):
        job = client.get('/api/browser-crawl/' + jid).json()
        if job['status'] == 'finished':
            return job
        time.sleep(.05)
    raise AssertionError('job did not finish')


def test_search_now_mixes_browser_and_api_sources(context, monkeypatch):
    main, client = context
    def fake_browser(q, platforms, profile_dir, on_event, **kw):
        assert platforms == ['bilibili'] and kw['sort'] == 'relevance'
        on_event('bilibili', 'reading', [PublicItem('哔哩哔哩作品', 'GPT 教程', 'GPT', 'https://www.bilibili.com/video/BV1x')], '读取')
        on_event('bilibili', 'done', [], '完成')
    monkeypatch.setattr(main, 'collect_in_browser', fake_browser)
    monkeypatch.setattr(main, 'fetch_hackernews', lambda q: [PublicItem('Hacker News', 'GPT', 'GPT help', 'https://news.ycombinator.com/item?id=9')])
    r = client.post('/api/search-now', json={'query': 'GPT', 'platforms': ['hackernews', 'bilibili'], 'sort': 'relevance'})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()['id'])
    assert job['platforms']['hackernews']['inserted'] == 1 and job['platforms']['hackernews']['name']
    assert job['platforms']['bilibili']['inserted'] == 1 and job['platforms']['bilibili']['status'] == 'done'
    assert client.get('/api/browser-crawl-active').json() == {}
    assert client.post('/api/search-now', json={'query': 'GPT', 'platforms': ['youtube']}).status_code == 422
    assert client.post('/api/search-now', json={'query': 'GPT', 'platforms': ['nope']}).status_code == 422


def test_search_now_api_error_is_reported(context, monkeypatch):
    main, client = context
    def broken(q):
        raise RuntimeError('429 Too Many Requests')
    monkeypatch.setattr(main, 'fetch_bluesky', broken)
    job = _wait(client, client.post('/api/search-now', json={'query': 'GPT', 'platforms': ['bluesky']}).json()['id'])
    assert job['platforms']['bluesky']['status'] == 'error' and '429' in job['platforms']['bluesky']['error']


def test_browser_login_opens_pages_and_releases_lock(context, monkeypatch):
    main, client = context
    seen = {}
    def fake_login(platforms, profile_dir, on_status, **kw):
        seen['platforms'] = platforms
        on_status('done', '登录窗口已关闭')
    monkeypatch.setattr(main, 'open_login_pages', fake_login)
    assert client.post('/api/browser-login', json={'platforms': ['wrong']}).status_code == 422
    assert client.post('/api/browser-login', json={'platforms': ['xhs', 'douyin']}).status_code == 202
    for _ in range(50):
        if client.get('/api/browser-login').json()['status'] != 'running':
            break
        time.sleep(.02)
    assert seen['platforms'] == ['xhs', 'douyin'] and client.get('/api/browser-login').json()['status'] == 'done'
    assert main.BROWSER_EXEC_LOCK.acquire(blocking=False)
    try:
        assert client.post('/api/browser-login', json={'platforms': ['xhs']}).status_code == 409
    finally:
        main.BROWSER_EXEC_LOCK.release()


def test_items_platform_and_kind_filters(context):
    main, client = context
    from app.browser_collector import comment_item
    url = 'https://www.douyin.com/video/42'
    main.save_items([PublicItem('抖音作品', 'GPT', 'GPT 视频', url),
                     comment_item('douyin', url, 'GPT', {'text': 'GPT 怎么买', 'author': 'a'}),
                     PublicItem('Reddit', 'GPT', 'GPT post', 'https://www.reddit.com/r/x/comments/1/')], manual_query='GPT')
    assert {x['source'] for x in client.get('/api/items?platform=抖音').json()} == {'抖音作品', '抖音评论'}
    assert [x['source'] for x in client.get('/api/items?platform=抖音&kind=comment').json()] == ['抖音评论']
    assert '抖音评论' not in {x['source'] for x in client.get('/api/items?kind=work').json()}
    plats = {r['platform']: r['count'] for r in client.get('/api/platforms').json()}
    assert plats['抖音'] == 2 and plats['Reddit'] == 1
