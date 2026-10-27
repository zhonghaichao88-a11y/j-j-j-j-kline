"""All network responses in this file are mocks, not live platform verification."""
from app import adapters
from app.adapters import PublicItem
from app.browser_collector import SUPPORTED, safe_post_url, search_url


def test_browser_extended_allowlist_and_links():
    examples = {
        'xhs': 'https://www.xiaohongshu.com/explore/ab12',
        'douyin': 'https://www.douyin.com/video/999001',
        'weibo': 'https://weibo.com/1234567/MabcQ',
        'zhihu': 'https://www.zhihu.com/question/1234/answer/5678',
        'bilibili': 'https://www.bilibili.com/video/BV1xx123abc',
        'kuaishou': 'https://www.kuaishou.com/short-video/abc123',
        'tiktok': 'https://www.tiktok.com/@example/video/710123456',
        'tieba': 'https://tieba.baidu.com/p/1234567',
    }
    assert set(examples) == set(SUPPORTED)
    for source, link in examples.items():
        assert safe_post_url(source,link)
        assert 'https://' in search_url(source, 'GPT 客服')
        assert not safe_post_url(source,link.replace('https://','http://'))
        assert not safe_post_url(source,link.replace('.com','.com.evil.example'))


def test_bluesky_real_post_parser(monkeypatch):
    def fake_get(url, **kw):
        assert 'public.api.bsky.app' in url and kw['params']['q'] == 'GPT'
        class R:
            def json(self):
                return {'posts':[{'uri':'at://did:plc:abc/app.bsky.feed.post/xyz123',
                        'author':{'handle':'bot.bsky.social','displayName':'公开用户'},
                        'record':{'text':'GPT在哪里可以购买？','createdAt':'2026-10-20T10:00:00Z'}}]}
        return R()
    monkeypatch.setattr(adapters,'_get',fake_get)
    result=adapters.fetch_bluesky('GPT')
    assert len(result)==1 and result[0].url=='https://bsky.app/profile/bot.bsky.social/post/xyz123'
    assert result[0].text.startswith('GPT')


def test_youtube_uses_official_video_and_comment_endpoints(monkeypatch):
    monkeypatch.setenv('YOUTUBE_API_KEY','test-key')
    called=[]
    def fake_get(url, **kw):
        called.append(url)
        class R:
            def json(self):
                if url.endswith('/search'):
                    return {'items':[{'id':{'videoId':'AaBbCcDd_01'},'snippet':{'title':'GPT 资讯', 'description':'公开作品','channelTitle':'创作者'}}]}
                if url.endswith('/videos'):
                    return {'items':[{'id':'AaBbCcDd_01','statistics':{'viewCount':'1200','likeCount':'34','commentCount':'5'}}]}
                return {'items':[{'snippet':{'topLevelComment':{'id':'CMT-A','snippet':{'textOriginal':'需要GPT服务报价','authorDisplayName':'买家','publishedAt':'2026-10-20T10:00:00Z'}}}}]}
        return R()
    monkeypatch.setattr(adapters,'_get',fake_get)
    rows=adapters.fetch_youtube('GPT',include_comments=True,max_videos=1,max_comments=3)
    assert [x.source for x in rows]==['YouTube视频','YouTube评论']
    assert '&lc=CMT-A' in rows[1].url and len(called)==3
    assert rows[0].metrics == {'view': 1200, 'like': 34, 'comment': 5}


def test_monitor_public_and_browser_combo_and_failure(context, monkeypatch):
    main, client=context
    sources=client.get('/api/monitor-sources').json()
    assert any(x['id']=='xhs' and x['configured'] for x in sources)
    assert any(x['id']=='bluesky' and x['mode']=='api' for x in sources)
    assert any(x['id']=='weibo' and x['mode']=='browser' for x in sources)
    assert any(x['id']=='authorized_xhs' and x['configured'] is False for x in sources)
    assert client.post('/api/monitors',json={'query':'GPT','platforms':['authorized_xhs']}).status_code==422
    def fake_browser(q, platforms, profile_dir, event, **kw):
        assert platforms == ['xhs']
        event('xhs','error',[],'需要重新登录')
    monkeypatch.setattr(main,'collect_in_browser',fake_browser)
    monkeypatch.setattr(main,'fetch_bluesky',lambda q:[PublicItem('Bluesky','需求','需要GPT客服报价','https://bsky.app/profile/a.bsky.social/post/p001')])
    # The scheduler can run independently in a TestClient; set the newly made monitor's due time far in the future
    r=client.post('/api/monitors',json={'query':'GPT','platforms':['xhs','bluesky']})
    assert r.status_code==201, r.text
    mid=r.json()['id']
    assert main._monitor_once(mid)
    status=client.get('/api/monitors').json()[0]
    assert status['last_status']=='partial'
    assert status['last_new']==1
    assert '需要重新登录' in status['last_error']
    logs=client.get('/api/monitor-runs').json()
    assert logs[0]['details']['bluesky']['status']=='done'
    assert logs[0]['details']['xhs']['status']=='error'
    assert main._monitor_once(mid)
    assert client.get('/api/monitors').json()[0]['last_new']==0


def test_github_comments_without_html_scraping(monkeypatch):
    monkeypatch.setattr(adapters,'fetch_github',lambda q:[PublicItem('GitHub','Issue: GPT','Need help with GPT','https://github.com/example/project/issues/23')])
    def fake_get(url, **kw):
        assert url=='https://api.github.com/repos/example/project/issues/23/comments'
        class R:
            def json(self): return [{'html_url':'https://github.com/example/project/issues/23#issuecomment-1','body':'报价是多少', 'user':{'login':'human'}}]
        return R()
    monkeypatch.setattr(adapters,'_get',fake_get)
    rows=adapters.fetch_github_with_comments('GPT')
    assert len(rows)==2 and rows[1].source=='GitHub评论'
