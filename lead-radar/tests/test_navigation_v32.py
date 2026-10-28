"""Navigation/locator regression tests. All source responses here are mocks.
These tests do not assert that a real website still exposes these DOM structures.
"""
from app import adapters as a
from app import browser_collector as b
from app.adapters import PublicItem
from hashlib import sha256
import pytest

SITES = {
 'xhs': 'https://www.xiaohongshu.com/explore/abc123',
 'douyin': 'https://www.douyin.com/video/987654321',
 'weibo': 'https://weibo.com/12345/ABC123',
 'zhihu': 'https://www.zhihu.com/question/111/answer/222',
 'bilibili': 'https://www.bilibili.com/video/BV1abc1234d',
 'kuaishou': 'https://www.kuaishou.com/short-video/abc123',
 'tiktok': 'https://www.tiktok.com/@sample/video/123123123',
 'tieba': 'https://tieba.baidu.com/p/11122233',
}

@pytest.mark.parametrize('platform,url',SITES.items())
def test_browser_comment_navigation_all_eight(platform,url):
    assert b.safe_post_url(platform,url)
    c=b.comment_item(platform,url,'采购问题',{'author':'User','text':'需要 GPT 客服方案报价','id':'4567'})
    if platform == 'bilibili':
        # bilibili's own reply anchor jumps to the comment (verified on the live site in 3.3)
        assert c.comment_url == url + '#reply4567' and c.url == c.comment_url and c.navigation_kind == 'direct'
        return
    assert c.post_url == url and c.url == url and c.comment_url == ''
    assert c.navigation_kind == 'assisted' and c.comment_id == '4567'
    assert c.dedup_token.startswith(url+'#lr-comment-')
    assert '#lr-comment-' not in c.url

@pytest.mark.parametrize('platform,url',SITES.items())
def test_browser_literal_permalink_only(platform,url):
    direct=url+'?comment_id=34567'
    assert b.safe_comment_permalink(platform,url,direct)
    assert b.comment_item(platform,url,'A',{'text':'需要设计系统','href':direct}).navigation_kind=='direct'
    assert not b.safe_comment_permalink(platform,url,'https://bad-site.example/post?comment_id=34567')
    assert not b.safe_comment_permalink(platform,url,url+'#lr-comment-34567')
    assert not b.safe_comment_permalink(platform,url,url+'?keyword=gpt')
    assert not b.safe_comment_permalink(platform,url,url.replace('https://','http://')+'?comment_id=34567')


def test_locator_js_has_per_platform_selectors():
    for platform in SITES:
        for js in (b.COMMENTS_DOM_JS,b.LOCATE_DOM_JS,b.SCROLL_COMMENTS_JS):
            assert platform+':' in js
    assert 'scrollIntoView' in b.LOCATE_DOM_JS
    assert "data-comment-id" in b.COMMENTS_DOM_JS
    assert 'innerText' in b.LOCATE_DOM_JS


def test_hackernews_discussion_comment_direct(monkeypatch):
    class Response:
        def json(self):return {'hits':[{'objectID':'256','comment_text':'GPT 代理如何购买？','author':'u'}, {'objectID':'257','title':'AI News'}, {'objectID':'257&evil=1','comment_text':'invalid'}]}
    monkeypatch.setattr(a,'_get',lambda *args,**kwargs:Response())
    rows=a.fetch_hackernews('GPT')
    assert len(rows)==2
    assert rows[0].source=='Hacker News评论'
    assert rows[0].comment_url=='https://news.ycombinator.com/item?id=256'
    assert rows[0].navigation_kind=='direct'
    assert rows[1].source=='Hacker News'


def test_authorized_comments_preserve_permalink_only_when_trusted(monkeypatch):
    monkeypatch.setenv('DOUYIN_API_URL','https://vendor.example/api')
    class Response:
        def json(self):return {'data':[
            {'type':'comment', 'content':'哪里有 GPT 服务','post_url':SITES['douyin'], 'comment_id':'101', 'comment_url':SITES['douyin']+'?comment_id=101'},
            {'type':'comment', 'content':'想了解GPT客服', 'post_url':SITES['douyin'], 'comment_url':'https://steal.test/capture?comment_id=1'},
            {'title':'GPT 视频', 'text':'测试', 'url':SITES['douyin']},
        ]}
    monkeypatch.setattr(a,'_get',lambda *args,**kwargs:Response())
    rows=a.fetch_platform('douyin','GPT')
    assert rows[0].source=='抖音评论' and rows[0].navigation_kind=='direct'
    assert rows[0].comment_url.endswith('comment_id=101') and rows[0].comment_id=='101'
    assert rows[1].navigation_kind=='manual' and rows[1].comment_url=='' and rows[1].url==SITES['douyin']
    assert rows[2].source=='抖音'


def test_authorized_unsupported_platform_never_claims_browser_locator(monkeypatch):
    monkeypatch.setenv('FACEBOOK_API_URL','https://vendor.example/search')
    class Response:
        def json(self):return [{'type':'reply','text':'GPT需要购买吗','parent_url':'https://www.facebook.com/example/posts/100', 'comment_id':'200'}]
    monkeypatch.setattr(a,'_get',lambda *args,**kwargs:Response())
    r=a.fetch_platform('facebook','GPT')[0]
    assert r.source=='Facebook评论' and r.navigation_kind=='manual'
    assert r.post_url.endswith('posts/100')


def test_bluesky_reply_url_is_own_direct_permalink(monkeypatch):
    class Response:
        def json(self):return {'posts':[{'uri':'at://did:plc:hello/app.bsky.feed.post/xy123','author':{'handle':'buyer.bsky.social'},'record':{'text':'需要 GPT 供应商','reply':{'parent':{'uri':'at://did:plc:someone/app.bsky.feed.post/p001'}}}}]}
    monkeypatch.setattr(a,'_get',lambda *args,**kwargs:Response())
    c=a.fetch_bluesky('GPT')[0]
    assert c.source=='Bluesky评论' and c.navigation_kind=='direct'
    assert c.comment_url.endswith('/post/xy123')


def test_upgrade_assisted_to_direct_keeps_notes(context):
    main,client=context
    url=SITES['xhs']
    c=b.comment_item('xhs',url,'GPT',{'author':'u','text':'需要GPT客服帮忙'})
    main.save_items([c],manual_query='GPT')
    rid=client.get('/api/items?source=小红书评论').json()[0]['id']
    assert client.patch(f'/api/items/{rid}',json={'notes':'已电话跟进','status':'contacted'}).status_code==200
    direct=b.comment_item('xhs',url,'GPT',{'author':'u','text':'需要GPT客服帮忙','href':url+'?comment_id=1001'})
    main.save_items([direct],manual_query='GPT')
    x=client.get(f'/api/items/{rid}').json()
    assert x['navigation_kind']=='direct' and x['comment_url'].endswith('comment_id=1001')
    assert x['notes']=='已电话跟进' and x['status']=='contacted'


def test_interface_labels_explain_limitations(context):
    main,client=context
    html=client.get('/').text
    assert '尝试高亮评论' in html and '打开评论直达链接' in html
    assert '暂不支持评论直达' in html
    assert '评论跳转说明' in html
    assert client.get('/api/health').json()['version']=='4.1.4'
