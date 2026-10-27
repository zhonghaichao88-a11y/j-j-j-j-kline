"""Comment navigation tests run without pretending to contact social platforms."""
from app.adapters import item_key
from app.browser_collector import comment_item, safe_comment_permalink, safe_post_url
from sqlalchemy import select


def test_browser_comment_no_fake_permalink_and_stable_legacy_key():
    url = 'https://www.douyin.com/video/123456789'
    row = comment_item('douyin', url, 'GPT', {'author': '一位用户', 'text': '找 GPT 客服供应商，求报价'})
    assert row.url == url
    assert row.post_url == url
    assert row.navigation_kind == 'assisted'
    assert row.comment_url == ''
    assert '#lr-comment-' not in row.url
    assert row.dedup_token.startswith(url + '#lr-comment-')
    from hashlib import sha256
    assert item_key(row) == sha256(row.dedup_token.encode()).hexdigest()


def test_only_literal_same_post_comment_link_can_be_direct():
    post = 'https://www.xiaohongshu.com/explore/abcdef123'
    direct = post + '?comment_id=12345'
    assert safe_comment_permalink('xhs',post,direct)
    assert not safe_comment_permalink('xhs',post,post+'#lr-comment-123')
    assert not safe_comment_permalink('xhs',post,'https://xiaohongshu.com.bad.test/explore/abcdef123?comment_id=123')
    assert not safe_comment_permalink('xhs',post,'https://www.xiaohongshu.com/explore/different?comment_id=123')
    x = comment_item('xhs',post,'测试',{'author':'张三','text':'需要报价', 'href':direct,'id':'012345'})
    assert x.navigation_kind=='direct'
    assert x.comment_url==direct
    assert x.comment_id=='012345'
    assert x.post_url==post


def test_migrate_old_fake_links_and_keep_notes(context):
    main,client=context
    with main.Session.begin() as s:
        obj=main.Item(external_id='legacy-fake-hash',source='小红书评论',title='评论',text='求报价',
                url='https://www.xiaohongshu.com/explore/abc123#lr-comment-fake',author='小A',
                notes='已经联系',status='contacted',created_at=main.NOW())
        s.add(obj)
        s.flush(); oid=obj.id
    main.init_db()
    x=client.get(f'/api/items/{oid}').json()
    assert x['url']=='https://www.xiaohongshu.com/explore/abc123'
    assert x['post_url']==x['url']
    assert x['navigation_kind']=='assisted'
    assert x['notes']=='已经联系' and x['status']=='contacted'


def test_browser_comment_saved_deduplicates_and_returns_navigation(context):
    main,client=context
    url='https://www.douyin.com/video/1234567'
    a=comment_item('douyin',url,'GPT',{'author':'甲','text':'找人做 GPT 客服，求报价'})
    b=comment_item('douyin',url,'GPT',{'author':'乙','text':'需要采购 GPT 客服系统'})
    assert main.save_items([a,b],manual_query='GPT')==(2,2)
    assert main.save_items([a,b],manual_query='GPT')==(2,0)
    rows=client.get('/api/items?source=抖音评论').json()
    assert len(rows)==2 and all(x['navigation_kind']=='assisted' for x in rows)
    assert all(x['url']==url and x['post_url']==url for x in rows)
    assert all('#lr-comment-' not in x['url'] for x in rows)


def test_comment_locator_endpoint_uses_local_browser_and_reports_status(context,monkeypatch):
    main,client=context
    url='https://www.douyin.com/video/1234567'
    a=comment_item('douyin',url,'GPT',{'author':'甲','text':'找人做 GPT 客服，求报价'})
    main.save_items([a],manual_query='GPT')
    item=client.get('/api/items?source=抖音评论').json()[0]
    seen={}
    def fake_locator(platform, post_url, text, author, profile_dir, callback, comment_id=""):
        seen["comment_id"] = comment_id
        seen.update(platform=platform,url=post_url,text=text,author=author)
        callback('located','test highlight')
    monkeypatch.setattr(main,'locate_visible_comment',fake_locator)
    response=client.post('/api/items/'+str(item['id'])+'/locate-comment')
    assert response.status_code==202
    import time
    status=None
    for _ in range(40):
        status=client.get('/api/comment-locate/'+response.json()['id']).json()
        if status['status']!='running':break
        time.sleep(.02)
    assert status['status']=='located'
    assert seen['platform']=='douyin' and seen['url']==url
    assert client.get('/api/comment-locate/bad-id').status_code==404
    assert client.post('/api/items/999999/locate-comment').status_code==404


def test_comment_buttons_render_and_not_fake_success(context):
    main,client=context
    html=client.get('/').text
    assert 'data-locate-comment' in html
    assert 'data-copy-comment' in html
    assert '尝试高亮评论' in html
    assert '不保证找到' in html
