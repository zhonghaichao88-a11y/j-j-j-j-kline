import pytest
from app import adapters as a

class Reply:
    def __init__(self, value=None, content=b''):
        self.value=value; self.content=content
    def json(self): return self.value


def test_normalized_json_formats():
    for data in [[{'title':'a'}], {'items':[{'title':'a'}]}, {'data':[{'title':'a'}]}, {'data':{'list':[{'title':'a'}]}}, {'results':[{'title':'a'}]}]:
        assert a._normalized_rows(data)==[{'title':'a'}]
    with pytest.raises(ValueError):a._normalized_rows({'error':'wrong'})


def test_authorized_source_with_list(monkeypatch):
    monkeypatch.setenv('XHS_API_URL', 'https://provider.example/api')
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply([{'title':'求推荐 AI','text':'寻找服务商','url':'https://example.com/post','author':{'nickname':'测试'}}]))
    out=a.fetch_platform('xhs','AI')
    assert out[0].author=='测试'
    assert out[0].source=='小红书'
    assert out[0].url=='https://example.com/post'


def test_github_public_api(monkeypatch):
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply({'items':[{'title':'求推荐 GPT','body':'求方案','html_url':'https://github.com/repo/issues/1','user':{'login':'bob'}}]}))
    assert a.fetch_github('GPT')[0].author=='bob'


def test_hn_public_api(monkeypatch):
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply({'hits':[{'objectID':'99','title':'AI startup','author':'hello'}]}))
    assert a.fetch_hackernews('AI')[0].url=='https://news.ycombinator.com/item?id=99'


def test_rss_and_atom(monkeypatch):
    rss=b'<rss version="2.0"><channel><item><title>GPT</title><description>Need AI</description><link>https://a.example/post</link></item></channel></rss>'
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply(content=rss))
    assert a.fetch_rss('https://a.example/feed')[0].title=='GPT'
    atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI</title><link href="https://a.example/1"/><summary>hello</summary></entry></feed>'
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply(content=atom))
    assert a.fetch_rss('https://a.example/atom')[0].url=='https://a.example/1'


def test_bad_feed(monkeypatch):
    monkeypatch.setattr(a, '_get', lambda *args, **kwargs: Reply(content=b'not xml'))
    with pytest.raises(ValueError): a.fetch_rss('https://a.example/feed')
