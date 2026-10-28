"""Monitor tests mock third-party pages; never pretend real XHS/Douyin are reachable."""
from datetime import datetime
from app.adapters import PublicItem


def test_monitor_create_validation_persisted_and_toggle(context):
    main, client = context
    assert client.post('/api/monitors', json={'query':'GPT','platforms':['xhs'],'interval_minutes':1}).status_code == 422
    assert client.post('/api/monitors', json={'query':'GPT','platforms':['xx']}).status_code == 422
    assert client.post('/api/monitors', json={'query':' ','platforms':['xhs']}).status_code == 422
    r = client.post('/api/monitors', json={'query':' GPT ', 'platforms':['xhs','douyin'], 'interval_minutes':5})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data['active'] and data['interval_minutes'] == 5
    assert data['platforms'] == ['xhs','douyin'] and data['query'] == 'GPT'
    assert len(client.get('/api/monitors').json()) == 1
    mid = data['id']
    paused = client.patch(f'/api/monitors/{mid}',json={'active':False})
    assert paused.status_code == 200
    assert not paused.json()['active']
    assert client.post(f'/api/monitors/{mid}/run').status_code == 409
    resumed = client.patch(f'/api/monitors/{mid}',json={'active':True})
    assert resumed.status_code == 200 and resumed.json()['active']
    assert client.post(f'/api/monitors/{mid}/run').json()['queued']
    assert client.delete(f'/api/monitors/{mid}').json()['deleted']
    assert client.get('/api/monitors').json() == []
    assert client.patch(f'/api/monitors/{mid}',json={'active':False}).status_code == 404


def test_monitor_first_discovery_then_duplicates_and_new_comment(context, monkeypatch):
    main, client = context
    batches = []
    def fake_browser(query, platforms, profile_dir, on_event, **kwargs):
        # 3.3: visible browser by default so the user can sign in / pass checks.
        assert query == 'GPT' and kwargs['headless'] is False
        assert kwargs['sort'] == 'latest' and kwargs['login_wait_seconds'] >= 30
        on_event('xhs', 'reading', [PublicItem('小红书作品', 'GPT 招商', '寻求 GPT 服务商报价',
            'https://www.xiaohongshu.com/explore/post1', '公开用户')], '已获取作品')
        on_event('xhs', 'reading', [PublicItem('小红书评论', '评论', 'GPT 具体多少钱',
            'https://www.xiaohongshu.com/explore/post1#lr-comment-a','用户甲')], '已获取评论')
        if batches:
            on_event('xhs', 'reading', [PublicItem('小红书评论', '评论', '请问谁能做GPT定制',
                'https://www.xiaohongshu.com/explore/post1#lr-comment-b','用户乙')], '新增回复')
        batches.append(True)
        on_event('xhs','done',[],'结束')
    monkeypatch.setattr(main, 'collect_in_browser',fake_browser)
    mid = client.post('/api/monitors',json={'query':'GPT','platforms':['xhs']}).json()['id']
    assert main._monitor_once(mid)
    first = client.get('/api/monitors').json()[0]
    assert first['last_status']=='ok' and first['last_new']==2
    assert first['last_fetched']==2
    assert first['last_success_at']
    assert datetime.fromisoformat(first['next_run_at']) > datetime.fromisoformat(first['last_run_at'])
    assert main._monitor_once(mid)
    later = client.get('/api/monitors').json()[0]
    assert later['last_new']==1 and later['last_fetched']==3 and later['run_count']==2
    assert len([x for x in client.get('/api/items?q=GPT').json() if x['source'] in ('小红书作品','小红书评论')])==3
    runlog = client.get('/api/monitor-runs').json()
    assert len(runlog)==2 and runlog[0]['inserted']==1 and runlog[1]['inserted']==2
    assert runlog[0]['details']['xhs']['status']=='done'


def test_monitor_reports_empty_as_error(context, monkeypatch):
    main, client = context
    def fake_browser(query, platforms, profile_dir, on_event, **kwargs):
        on_event('douyin','empty',[],'登录失败，页面没有内容')
    monkeypatch.setattr(main,'collect_in_browser',fake_browser)
    mid = client.post('/api/monitors',json={'query':'GPT','platforms':['douyin']}).json()['id']
    main._monitor_once(mid)
    m=client.get('/api/monitors').json()[0]
    assert m['last_status']=='error' and '登录失败' in m['last_error']
    assert m['last_new']==0 and m['last_success_at']==''


def test_monitor_shared_browser_cannot_run_concurrently(context):
    main, client = context
    mid=client.post('/api/monitors',json={'query':'GPT','platforms':['xhs']}).json()['id']
    assert main.BROWSER_EXEC_LOCK.acquire(blocking=False)
    try:
        assert main._monitor_once(mid) is False
    finally:
        main.BROWSER_EXEC_LOCK.release()
    assert client.get('/api/monitor-runs').json()==[]


def test_monitor_interface_and_health(context):
    main, client=context
    assert 'id="monitorPanel"' in client.get('/').text
    assert 'id="monitorForm"' in client.get('/').text
    assert client.get('/api/health').json()['version']=='4.1.4'


def test_background_scheduler_processes_created_due_monitor(context, monkeypatch):
    import time
    main, client=context
    def fake_browser(query, platforms, profile_dir, on_event, **kwargs):
        on_event('xhs','reading',[PublicItem('小红书作品','GPT 用户讨论','我想用GPT做智能客服',
                     'https://www.xiaohongshu.com/explore/auto-test','公开作者')],'读取')
        on_event('xhs','done',[],'完成')
    monkeypatch.setattr(main, 'collect_in_browser',fake_browser)
    mid=client.post('/api/monitors',json={'query':'GPT','platforms':['xhs'],'interval_minutes':5}).json()['id']
    deadline=time.monotonic()+6
    while time.monotonic()<deadline:
        m=client.get('/api/monitors').json()[0]
        if m['last_run_id']:
            break
        time.sleep(.1)
    assert m['last_run_id'] and m['last_status']=='ok'
    assert m['last_new']==1
