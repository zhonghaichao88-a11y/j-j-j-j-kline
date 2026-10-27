def test_manual_xhs_can_save_and_deduplicate(context):
    main, client = context
    item = {"platform":"xhs", "url":"https://www.xiaohongshu.com/explore/abcdef123", "title":"找装修团队", "text":"最近要装修，急需施工团队报价", "author":"公开昵称"}
    r=client.post('/api/platforms/manual-save',json=item)
    assert r.status_code == 201, r.text
    assert r.json()['inserted']==1
    assert r.json()['item']['source']=='小红书'
    assert r.json()['item']['url']==item['url']
    r2=client.post('/api/platforms/manual-save',json=item)
    assert r2.status_code==201 and r2.json()['duplicate'] is True
    assert len(client.get('/api/items?source=小红书').json())==1


def test_manual_douyin_can_save(context):
    _, client=context
    data={'platform':'douyin','url':'https://www.douyin.com/video/1234567890','text':'我需要做一个小程序，价格多少？'}
    assert client.post('/api/platforms/manual-save',json=data).json()['source']=='抖音'
    assert len(client.get('/api/items?source=抖音').json())==1


def test_manual_rejects_invalid_domains(context):
    _,client=context
    base={'platform':'xhs', 'text':'想找装修公司', 'url':'https://evilxiaohongshu.com/explore/123'}
    assert client.post('/api/platforms/manual-save',json=base).status_code==422
    base['url']='http://www.xiaohongshu.com/explore/123'
    assert client.post('/api/platforms/manual-save',json=base).status_code==422
    base['url']='https://127.0.0.1/explore/123'
    assert client.post('/api/platforms/manual-save',json=base).status_code==422
    base['platform']='unknown'
    assert client.post('/api/platforms/manual-save',json=base).status_code==422


def test_cn_ui_does_not_claim_automatic_crawling(context):
    _,client=context
    page=client.get('/').text
    assert 'id="cnManualForm"' in page
    assert "立即搜索一次" in page and "开启持续监控" in page
