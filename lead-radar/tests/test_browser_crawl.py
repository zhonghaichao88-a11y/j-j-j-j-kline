from pathlib import Path
from app.browser_collector import search_url, safe_post_url, comment_item, MAX_POSTS, MAX_COMMENTS_PER_POST


def test_search_urls_are_normal_urls():
    assert 'GPT' in search_url('xhs', 'GPT')
    assert 'GPT' in search_url('douyin', 'GPT')
    assert '%E6%9C%8D' in search_url('xhs', '客服')


def test_posts_must_be_real_platform_links():
    assert safe_post_url('xhs', 'https://www.xiaohongshu.com/explore/abcdef')
    assert safe_post_url('xhs', 'https://www.xiaohongshu.com/search_result/abc123?xsec_token=t')
    assert safe_post_url('douyin', 'https://www.douyin.com/video/123123123')
    assert not safe_post_url('xhs', 'https://xiaohongshu.com.evil.net/explore/123')
    assert not safe_post_url('douyin', 'http://www.douyin.com/video/123123')
    assert not safe_post_url('douyin', 'https://www.douyin.com/search/GPT')


def test_comment_dedup_is_distinct_per_comment():
    url = 'https://www.douyin.com/video/1234567'
    a = comment_item('douyin', url, 'GPT 讨论', {'author':'甲','text':'想找专业团队做 GPT 客服'})
    b = comment_item('douyin', url, 'GPT 讨论', {'author':'乙','text':'需要报价'})
    assert a.source == '抖音评论'
    assert a.url == b.url == url  # real navigation opens the work, never a fake #fragment
    assert a.dedup_token != b.dedup_token
    assert a.navigation_kind == 'assisted'
    assert a.post_url == url
    assert not comment_item('xhs', url, '标题', {'text': ' '})


def test_browser_api_jobs_with_simulated_visible_content(context, monkeypatch):
    import time
    main, client = context
    # Fake the browser itself; no claim of external platform verification.
    def fake_browser(q, platforms, profile_dir, on_event, **kwargs):
        assert q == 'GPT'
        assert kwargs['max_posts'] <= MAX_POSTS
        assert kwargs['comments_per_post'] <= MAX_COMMENTS_PER_POST
        from app.adapters import PublicItem
        for p in platforms:
            on_event(p, 'reading', [PublicItem('小红书作品', 'GPT 怎么用', '求报价做 GPT 客服',
                  'https://www.xiaohongshu.com/explore/abc123', 'user')], '读取中')
            on_event(p, 'done', [], '完成')
    monkeypatch.setattr(main, 'collect_in_browser', fake_browser)
    assert client.post('/api/browser-crawl/start', json={'query':'  ', 'platforms':['xhs']}).status_code == 422
    assert client.post('/api/browser-crawl/start', json={'query':'GPT', 'platforms':['wrong']}).status_code == 422
    response = client.post('/api/browser-crawl/start', json={'query':'GPT','platforms':['xhs']})
    assert response.status_code == 202
    jid = response.json()['id']
    for _ in range(40):
        job=client.get('/api/browser-crawl/'+jid).json()
        if job['status']=='finished':break
        time.sleep(.05)
    assert job['status']=='finished' and not job['error']
    assert job['platforms']['xhs']['inserted']==1
    assert len(client.get('/api/items?q=GPT').json())>=1
    assert client.get('/api/browser-crawl/doesnotexist').status_code==404


def test_visible_browser_ui(context):
    main, client=context
    page=client.get('/').text
    assert 'id="searchNowButton"' in page
    assert 'id="browserPlatforms"' in page and 'id="loginButton"' in page
    assert '浏览器' in page
