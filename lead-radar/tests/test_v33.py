"""3.3 regressions: canonical de-dup, metrics, profiles, authors, monitor scheduling.
All third-party responses are mocks; live-site behaviour is not asserted here."""
import time
import pytest
import requests
from app import adapters as a
from app import browser_collector as b
from app.adapters import PublicItem


def test_xhs_xsec_token_does_not_create_duplicates(context):
    main, client = context
    first = 'https://www.xiaohongshu.com/search_result/66aa11?xsec_token=AAA&xsec_source='
    second = 'https://www.xiaohongshu.com/explore/66aa11?xsec_token=BBB'
    def work(url):
        return PublicItem('小红书作品', 'GPT 求推荐', 'GPT 求推荐', url,
                          dedup_token=b.canonical_post_url('xhs', url))
    c1 = b.comment_item('xhs', first, 'GPT', {'text': 'GPT 多少钱', 'author': 'u1'})
    c2 = b.comment_item('xhs', second, 'GPT', {'text': 'GPT 多少钱', 'author': 'u1'})
    assert main.save_items([work(first), c1], manual_query='GPT') == (2, 2)
    assert main.save_items([work(second), c2], manual_query='GPT') == (2, 0)
    # navigation keeps the token the site needs
    saved = client.get('/api/items?source=小红书作品').json()[0]
    assert 'xsec_token=AAA' in saved['url']


def test_canonical_and_locale_prefix():
    assert b.safe_post_url('bilibili', 'https://www.bilibili.com/zh-Hans/video/BV1TQer6EEAT/')
    assert b.canonical_post_url('bilibili', 'https://www.bilibili.com/zh-Hans/video/BV1TQer6EEAT/?spm=1') == \
        'https://www.bilibili.com/video/BV1TQer6EEAT'
    assert b.canonical_post_url('xhs', 'https://www.xiaohongshu.com/discovery/item/abc?x=1') == \
        'https://www.xiaohongshu.com/explore/abc'
    assert not b.safe_post_url('bilibili', 'https://www.bilibili.com/zh-Hans/evil/BV1')


@pytest.mark.parametrize('platform,url,expected', [
    ('bilibili', 'https://space.bilibili.com/zh-Hans/319680380/?spm_id_from=x', 'https://space.bilibili.com/319680380'),
    ('xhs', 'https://www.xiaohongshu.com/user/profile/5f00aa?xsec_token=T', 'https://www.xiaohongshu.com/user/profile/5f00aa?xsec_token=T'),
    ('douyin', 'https://www.douyin.com/user/MS4wLjABAAAAxyz', 'https://www.douyin.com/user/MS4wLjABAAAAxyz'),
    ('weibo', 'https://weibo.com/u/1234567', 'https://weibo.com/u/1234567'),
    ('zhihu', 'https://www.zhihu.com/people/abc-1', 'https://www.zhihu.com/people/abc-1'),
    ('tiktok', 'https://www.tiktok.com/@some.user', 'https://www.tiktok.com/@some.user'),
    ('tieba', 'https://tieba.baidu.com/home/main?id=tb.1.abc&fr=pb', 'https://tieba.baidu.com/home/main?id=tb.1.abc'),
    ('douyin', 'https://evil.example/user/MS4wLjABAAAAxyz', ''),
    ('bilibili', 'https://www.bilibili.com/319680380', ''),
    ('xhs', 'http://www.xiaohongshu.com/user/profile/5f00aa', ''),
])
def test_profile_urls(platform, url, expected):
    assert b.safe_profile_url(platform, url) == expected


@pytest.mark.parametrize('raw,n', [('1.2万', 12000), ('3.4w', 34000), ('10万+', 100000), ('1,234', 1234),
                                    ('赞同 2,345', 2345), ('2.1K', 2100), ('76', 76), ('1.5亿', 150000000),
                                    ('点赞', None), (None, None), (12, 12)])
def test_count_parsing(raw, n):
    assert a.to_count(raw) == n


def test_bilibili_comment_fields():
    c = b.comment_item('bilibili', 'https://www.bilibili.com/zh-Hans/video/BV1TQer6EEAT/', 'GPT',
                       {'text': '求 GPT 教程', 'author': '甲', 'id': '317240233857', 'ctime': 1792150024,
                        'authorHref': 'https://space.bilibili.com/zh-Hans/319680380', 'like': 5})
    assert c.navigation_kind == 'direct' and c.comment_url.endswith('/BV1TQer6EEAT/#reply317240233857')
    assert c.published_at.startswith('2026-') and c.author_url == 'https://space.bilibili.com/319680380'
    assert c.metrics == {'like': 5}


def test_metrics_and_profile_saved_and_refreshed(context):
    main, client = context
    url = 'https://www.douyin.com/video/777'
    item = PublicItem('抖音作品', 'GPT 教程', 'GPT 客服教程', url, '作者A', author_url='https://www.douyin.com/user/MS4wLjABAAAA1',
                      metrics={'like': 10, 'collect': 2}, dedup_token=b.canonical_post_url('douyin', url))
    main.save_items([item], manual_query='GPT')
    later = PublicItem('抖音作品', 'GPT 教程', 'GPT 客服教程', url, '作者A', metrics={'like': 25, 'share': 3},
                       dedup_token=b.canonical_post_url('douyin', url))
    assert main.save_items([later], manual_query='GPT') == (1, 0)
    x = client.get('/api/items?source=抖音作品').json()[0]
    assert x['metrics'] == {'like': 25, 'collect': 2, 'share': 3}
    assert x['author_url'].endswith('MS4wLjABAAAA1') and x['metrics_updated_at']
    csv = client.get('/api/export.csv').text
    assert 'author_url' in csv.splitlines()[0] and ',25,' in csv


def test_authors_endpoint_groups_public_posters_and_commenters(context):
    main, client = context
    post = 'https://www.bilibili.com/video/BV1abc'
    rows = [
        PublicItem('哔哩哔哩作品', 'GPT 求推荐', '求推荐 GPT 客服', post, 'UP主', author_url='https://space.bilibili.com/1',
                   metrics={'like': 40}, dedup_token=post),
        b.comment_item('bilibili', post, 'GPT', {'text': 'GPT 多少钱？求报价', 'author': '买家', 'id': '11',
                                                  'authorHref': 'https://space.bilibili.com/2'}),
        b.comment_item('bilibili', post, 'GPT', {'text': '我也想要 GPT', 'author': '买家', 'id': '12',
                                                  'authorHref': 'https://space.bilibili.com/2'}),
    ]
    main.save_items(rows, manual_query='GPT')
    data = client.get('/api/authors?q=GPT').json()
    buyer = next(r for r in data if r['author'] == '买家')
    assert buyer['comments'] == 2 and buyer['works'] == 0 and buyer['author_url'] == 'https://space.bilibili.com/2'
    assert buyer['high_intent'] >= 1 and data[0]['author'] == '买家'  # high intent sorted first
    up = next(r for r in data if r['author'] == 'UP主')
    assert up['works'] == 1 and up['likes_received'] == 40 and up['platform'] == '哔哩哔哩'
    assert client.get('/api/authors?platform=抖音').json() == []
    assert '买家' in client.get('/api/authors.csv?q=GPT').text


def test_manual_save_all_browser_platforms(context):
    main, client = context
    r = client.post('/api/platforms/manual-save', json={'platform': 'weibo', 'url': 'https://weibo.com/123/AbC',
                                                      'text': 'GPT 求推荐'})
    assert r.status_code == 201 and r.json()['source'] == '微博'


def test_api_only_monitor_runs_while_browser_busy(context, monkeypatch):
    main, client = context
    monkeypatch.setattr(main, 'fetch_hackernews', lambda q: [PublicItem('Hacker News', 'GPT', 'need GPT help',
                                                                         'https://news.ycombinator.com/item?id=1')])
    mid = client.post('/api/monitors', json={'query': 'GPT', 'platforms': ['hackernews']}).json()['id']
    assert main.BROWSER_EXEC_LOCK.acquire(blocking=False)
    try:
        assert main._monitor_once(mid) is True
    finally:
        main.BROWSER_EXEC_LOCK.release()
    assert client.get('/api/monitors').json()[0]['last_status'] == 'ok'


def test_monitor_crash_is_not_stuck_running(context, monkeypatch):
    main, client = context
    def boom(*args, **kwargs):
        raise RuntimeError('意外错误')
    monkeypatch.setattr(main, '_run_monitor', boom)
    mid = client.post('/api/monitors', json={'query': 'GPT', 'platforms': ['hackernews']}).json()['id']
    with pytest.raises(RuntimeError):
        main._monitor_once(mid)
    m = client.get('/api/monitors').json()[0]
    assert m['last_status'] == 'error' and '意外错误' in m['last_error']
    assert client.post(f'/api/monitors/{mid}/run').status_code == 200


def test_monitor_sort_and_limits(context, monkeypatch):
    main, client = context
    seen = {}
    def fake_browser(q, platforms, profile_dir, on_event, **kw):
        seen.update(kw)
        on_event('bilibili', 'waiting_login', [], '请登录')
        on_event('bilibili', 'done', [], '完成')
    monkeypatch.setattr(main, 'collect_in_browser', fake_browser)
    r = client.post('/api/monitors', json={'query': 'GPT', 'platforms': ['bilibili'], 'sort': 'relevance',
                                           'max_posts': 20, 'max_comments': 30})
    assert r.status_code == 201 and r.json()['sort'] == 'relevance'
    assert client.post('/api/monitors', json={'query': 'GPT', 'platforms': ['bilibili'], 'max_posts': 21}).status_code == 422
    main._monitor_once(r.json()['id'])
    assert seen['sort'] == 'relevance' and seen['max_posts'] == 20 and seen['comments_per_post'] == 30
    # a login prompt that the user resolved is not reported as a failure
    assert client.get('/api/monitors').json()[0]['last_status'] == 'ok'


def test_browser_sources_unavailable_without_playwright(context, monkeypatch):
    main, client = context
    monkeypatch.setattr(main, 'browser_available', lambda: False)
    rows = {r['id']: r for r in client.get('/api/monitor-sources').json()}
    assert rows['xhs']['configured'] is False and 'start.bat' in rows['xhs']['note']
    assert client.post('/api/monitors', json={'query': 'GPT', 'platforms': ['xhs']}).status_code == 422


def test_dedup_migration_rekeys_old_xhs_rows(context):
    main, client = context
    import hashlib
    old_url = 'https://www.xiaohongshu.com/search_result/abc1?xsec_token=OLD'
    with main.Session.begin() as s:
        s.add(main.Item(external_id=hashlib.sha256(old_url.encode()).hexdigest(), source='小红书作品', title='GPT',
                        text='GPT', url=old_url, author='', notes='保留', status='contacted', created_at=main.NOW()))
        meta = s.get(main.SchemaMeta, 'dedup_version')
        s.delete(meta)
    main.init_db()
    new = PublicItem('小红书作品', 'GPT', 'GPT', 'https://www.xiaohongshu.com/explore/abc1?xsec_token=NEW',
                     dedup_token=b.canonical_post_url('xhs', 'https://www.xiaohongshu.com/explore/abc1?xsec_token=NEW'))
    assert main.save_items([new], manual_query='GPT') == (1, 0)
    x = client.get('/api/items?source=小红书作品').json()
    assert len(x) == 1 and x[0]['notes'] == '保留'


def test_bing_redirect_followed_only_within_bing(monkeypatch):
    calls = []
    class R:
        def __init__(self, url, code, loc=''):
            self.url, self.status_code, self.headers, self.content = url, code, {'Location': loc}, b'<rss><channel></channel></rss>'
        def raise_for_status(self):
            pass
    def fake_get(url, **kw):
        calls.append(url)
        if 'www.bing.com' in url:
            return R(url, 302, 'https://cn.bing.com/news/search?q=GPT&format=rss')
        if 'evil' in url:
            return R(url, 200)
        return R(url, 200)
    monkeypatch.setattr(a.requests, 'get', fake_get)
    assert a.fetch_bing_news('GPT') == [] and calls[-1].startswith('https://cn.bing.com/')
    def to_evil(url, **kw):
        return R(url, 302, 'https://evil.example/rss')
    monkeypatch.setattr(a.requests, 'get', to_evil)
    with pytest.raises(ValueError):
        a.fetch_bing_news('GPT')


def test_reddit_comment_block_keeps_posts(monkeypatch):
    post = a.PublicItem('Reddit', 'GPT', 'x', 'https://www.reddit.com/r/test/comments/abc/title/')
    monkeypatch.setattr(a, 'fetch_reddit_json', lambda q: [post])
    def blocked(*args, **kw):
        raise requests.HTTPError('429')
    monkeypatch.setattr(a, '_get', blocked)
    assert a.fetch_reddit_with_comments('GPT', max_posts=3) == [post]


def test_ui_has_v33_controls(context):
    main, client = context
    html = client.get('/').text
    for marker in ('id="authorsPanel"', 'id="optSort"', 'id="optInterval"', 'id="optPosts"', 'metricsMarkup', '/api/authors',
                   'data-tab="leads"', 'data-tab="people"', '/api/search-now', '/api/browser-login'):
        assert marker in html


def test_old_html_entities_are_decoded_once(context):
    main, client = context
    with main.Session.begin() as s:
        s.add(main.Item(external_id='hn-old', source='Hacker News评论', title='t', text='it&#x27;s 4 PWh&#x2F;yr',
                        url='https://news.ycombinator.com/item?id=5', created_at=main.NOW()))
        s.delete(s.get(main.SchemaMeta, 'html_unescape'))
    main.init_db()
    assert client.get('/api/items?q=PWh').json()[0]['text'] == "it's 4 PWh/yr"


def test_launch_falls_back_to_installed_edge(tmp_path):
    calls = []
    class FakeChromium:
        def launch_persistent_context(self, **kw):
            calls.append(kw.get('channel'))
            if kw.get('channel') is None:
                raise Exception("BrowserType.launch_persistent_context: Executable doesn't exist at C:/ms-playwright/chrome.exe")
            return 'ctx'
    class FakePW:
        chromium = FakeChromium()
    assert b._launch(FakePW(), tmp_path) == 'ctx' and calls == [None, 'msedge']


def test_launch_profile_in_use_is_not_hidden(tmp_path):
    class FakeChromium:
        def launch_persistent_context(self, **kw):
            raise Exception('Target page, context or browser has been closed: profile is locked')
    class FakePW:
        chromium = FakeChromium()
    with pytest.raises(RuntimeError, match='另一个'):
        b._launch(FakePW(), tmp_path)
