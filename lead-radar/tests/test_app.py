import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'radar.sqlite'))
    monkeypatch.setenv('SEED_DEMO', '1')
    monkeypatch.delenv('APP_PASSWORD', raising=False)
    monkeypatch.delenv('YOUTUBE_API_KEY', raising=False)
    monkeypatch.delenv('X_BEARER_TOKEN', raising=False)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as client:
        yield main, client
    main.engine.dispose()


def test_home_health_and_seed(context):
    m, client = context
    assert client.get('/').status_code == 200
    assert 'LEAD RADAR' in client.get('/').text
    assert client.get('/api/health').json()['ok'] is True
    stats = client.get('/api/stats').json()
    assert stats['total'] == 3 and stats['demo'] == 3 and stats['real'] == 0
    assert stats['high'] == 1


def test_classify_negation_and_purchase(context):
    m, _ = context
    assert m.classify('今天学习了，没有购买需求。')[0] == 'low'
    assert m.classify('求推荐客服，有没有靠谱团队和预算参考？')[0] == 'high'
    assert m.classify('求推荐 AI 客服，有预算，请报价')[0] == 'high'
    assert m.classify('Not buying anything this month.')[0] == 'low'
    assert m.classify('正在比较几个供应商方案')[0] == 'medium'


def test_kw_add_edit_toggle_delete(context):
    m, client = context
    r = client.post('/api/keywords', json={'name': '  测试 ', 'terms': ['  买电脑 ', '买电脑'], 'excludes': ['招聘']})
    assert r.status_code == 201
    k = r.json()
    assert k['terms'] == ['买电脑']
    assert client.patch('/api/keywords/' + str(k['id']), json={'active': False}).json()['active'] is False
    assert client.patch('/api/keywords/' + str(k['id']), json={'terms': []}).status_code == 422
    assert client.delete('/api/keywords/' + str(k['id'])).status_code == 200
    assert client.delete('/api/keywords/' + str(k['id'])).status_code == 404
    assert client.post('/api/keywords', json={'name': 'no', 'terms': ['  ']}).status_code == 422


def test_ingest_demo_deduped(context):
    m, client = context
    r = client.post('/api/ingest', json={'source': 'demo'})
    assert r.status_code == 200
    data = r.json()
    assert data['matched'] == 3
    assert data['inserted'] == 0
    assert data['duplicates'] == 3
    assert client.get('/api/ingest-logs').json()[0]['source'] == 'demo'


def test_notes_preserved_by_status(context):
    m, client = context
    item = client.get('/api/items').json()[0]
    pid = item['id']
    assert client.patch(f'/api/items/{pid}', json={'notes': '明天再联系'}).json()['notes'] == '明天再联系'
    updated = client.patch(f'/api/items/{pid}', json={'status': 'contacted'}).json()
    assert updated['status'] == 'contacted'
    assert updated['notes'] == '明天再联系'
    assert client.get(f'/api/items/{pid}').json()['notes'] == '明天再联系'
    assert client.patch(f'/api/items/{pid}', json={'status': 'invalid'}).status_code == 422
    assert client.patch('/api/items/99999', json={'status': 'saved'}).status_code == 404


def test_filter_pagination(context):
    m, client = context
    first = client.get('/api/items', params={'limit': 1, 'offset': 0}).json()
    next_one = client.get('/api/items', params={'limit': 1, 'offset': 1}).json()
    assert len(first) == len(next_one) == 1
    assert first[0]['id'] != next_one[0]['id']
    assert all(x['intent'] == 'low' for x in client.get('/api/items', params={'intent':'low'}).json())
    assert client.get('/api/items?limit=-1').status_code == 422


def test_unconfigured_source_and_invalid(context):
    m, client = context
    assert client.post('/api/ingest', json={'source':'not-real'}).status_code == 400
    assert client.post('/api/ingest', json={'source':'youtube', 'query':'AI'}).status_code == 400
    assert client.post('/api/ingest', json={'source':'github'}).status_code == 400
    assert client.post('/api/ingest', json={'source':'rss'}).status_code == 400
    assert client.post('/api/ingest', json={'source':'rss','url':'http://127.0.0.1/private'}).status_code == 400


def test_source_public_real_mock(context, monkeypatch):
    m, client = context
    from app.adapters import PublicItem
    monkeypatch.setattr(m, 'fetch_github', lambda q: [PublicItem('GitHub','求推荐 GPT 方案','有什么团队？','https://github.com/example/issues/1')])
    r = client.post('/api/ingest', json={'source':'github','query':'GPT'})
    assert r.json()['inserted'] == 1
    r2 = client.post('/api/ingest', json={'source':'github','query':'GPT'})
    assert r2.json()['inserted'] == 0
    assert client.get('/api/stats').json()['real'] == 1
    assert client.get('/api/items?source=GitHub').json()[0]['source'] == 'GitHub'


def test_ingest_all_configured_sources_mock(context, monkeypatch):
    m, client = context
    monkeypatch.setenv('DOUYIN_API_URL', 'https://authorized.example/api')
    monkeypatch.setattr(m, 'fetch_platform', lambda source, query: [])
    res=client.post('/api/ingest-all', params={'query':'GPT'}).json()
    assert res['configured_sources'] == 1
    assert res['results'][0]['source'] == 'douyin'


def test_ingest_error_logged(context, monkeypatch):
    m, client = context
    def fail(q):
        raise RuntimeError('mock permission denied')
    monkeypatch.setattr(m, 'fetch_hackernews', fail)
    r=client.post('/api/ingest', json={'source':'hackernews','query':'GPT'})
    assert r.status_code == 502
    assert 'mock permission denied' in client.get('/api/ingest-logs').json()[0]['error']


def test_csv_formula_guard(context):
    m, client = context
    from app.adapters import PublicItem
    m.save_items([PublicItem('Test', '=2+2 GPT', '购买 GPT', 'https://example.com/xyz')])
    text=client.get('/api/export.csv').content.decode('utf-8-sig')
    assert "'=2+2 GPT" in text
    assert '购买 GPT' in text


def test_trends_and_sources(context):
    m, client = context
    trend=client.get('/api/trends').json()
    assert trend['sources']['Demo']==3
    src=client.get('/api/sources').json()
    assert next(x for x in src if x['id']=='github')['configured']
    assert not next(x for x in src if x['id']=='youtube')['configured']


def test_basic_password(monkeypatch, tmp_path):
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(tmp_path / 'secured.sqlite'))
    monkeypatch.setenv('APP_PASSWORD', 'local-secret')
    import app.main as m
    importlib.reload(m)
    with TestClient(m.app) as client:
        assert client.get('/').status_code == 401
        assert client.get('/', auth=('admin','local-secret')).status_code == 200
        assert client.get('/api/health').status_code == 200
    m.engine.dispose()


def test_migrate_original_db(tmp_path, monkeypatch):
    filename=tmp_path / 'old.sqlite'
    con=sqlite3.connect(filename)
    con.execute("CREATE TABLE keywords (id INTEGER PRIMARY KEY, name TEXT NOT NULL, terms TEXT NOT NULL, excludes TEXT DEFAULT '', active INTEGER DEFAULT 1, created_at TEXT)")
    con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, external_id TEXT UNIQUE, source TEXT, title TEXT, text TEXT, url TEXT, author TEXT, published_at TEXT, matched_terms TEXT, intent TEXT, score INTEGER, status TEXT DEFAULT 'new', notes TEXT DEFAULT '', created_at TEXT)")
    con.execute("INSERT INTO keywords (name,terms,excludes,active) VALUES ('old','[\"GPT\"]','[]',1)")
    con.execute("INSERT INTO items (external_id,source,title,text,matched_terms,score) VALUES ('abc','Test','old','GPT','[\"GPT\"]',50)")
    con.commit();con.close()
    monkeypatch.setenv('DATABASE_URL', 'sqlite:///' + str(filename))
    import app.main as m
    importlib.reload(m)
    with TestClient(m.app) as client:
        assert client.get('/api/stats').json()['total']==1
        assert client.get('/api/items').json()[0]['title']=='old'
    m.engine.dispose()
