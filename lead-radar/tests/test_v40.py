"""4.0: new public sources, buyer/seller intent, lead workflow, settings, notifications."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from app import adapters as a
from app.adapters import PublicItem
from app.intent import analyze, parse_cn_time

ROOT = Path(__file__).resolve().parent.parent


class Reply:
    def __init__(self, payload=None, content=b"", status=200):
        self.payload, self.content, self.status_code = payload, content, status
        self.text = content.decode("utf-8", "replace") if content else ""

    def json(self):
        return self.payload


def http_error(code):
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code} error", response=resp)


# ---------------------------------------------------------------- sources
def test_stackexchange_questions(monkeypatch):
    seen = []

    def fake(url, **kw):
        seen.append(kw["params"]["site"])
        return Reply({"items": [{"title": "Looking for a CRM &amp; helpdesk", "body": "<p>Any recommendations?</p>",
                                 "link": "https://stackoverflow.com/questions/1/x", "creation_date": 1790000000,
                                 "owner": {"display_name": "Ann", "link": "https://stackoverflow.com/users/1/ann"},
                                 "score": 3, "answer_count": 2, "view_count": 40}]})
    monkeypatch.setenv("STACKEXCHANGE_SITES", "stackoverflow,softwarerecs")
    monkeypatch.setattr(a, "_get", fake)
    rows = a.fetch_stackexchange("crm")
    assert seen == ["stackoverflow", "softwarerecs"]
    assert rows[0].source == "Stack Overflow" and rows[1].source == "StackExchange·softwarerecs"
    assert rows[0].title == "Looking for a CRM & helpdesk" and rows[0].text == "Any recommendations?"
    assert rows[0].author_url.startswith("https://stackoverflow.com/users/")
    assert rows[0].metrics == {"like": 3, "comment": 2, "view": 40}


def test_stackexchange_all_failed_raises(monkeypatch):
    monkeypatch.setattr(a, "_get", lambda *x, **k: Reply({"error_message": "throttle violation"}))
    with pytest.raises(RuntimeError, match="throttle"):
        a.fetch_stackexchange("crm")


def test_lemmy_posts_and_comments(monkeypatch):
    monkeypatch.setattr(a, "_get", lambda url, **kw: Reply({
        "posts": [{"post": {"name": "Need a CRM", "body": "for my shop", "ap_id": "https://lemmy.ml/post/5",
                            "published": "2026-10-01T00:00:00Z"},
                   "creator": {"name": "bob", "actor_id": "https://lemmy.ml/u/bob"}, "counts": {"score": 4, "comments": 1}}],
        "comments": [{"comment": {"id": 9, "content": "try X", "ap_id": "https://lemmy.ml/comment/9"},
                      "post": {"name": "Need a CRM", "ap_id": "https://lemmy.ml/post/5"}, "creator": {"name": "eve"}}]}))
    post, comment = a.fetch_lemmy("crm")
    assert post.source == "Lemmy" and post.url == "https://lemmy.ml/post/5" and post.metrics["comment"] == 1
    assert comment.source == "Lemmy评论" and comment.navigation_kind == "direct" and comment.post_url == post.url


def test_mastodon_hashtag_without_token(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append(url)
        return Reply([{"url": "https://mastodon.social/@a/1", "content": "<p>Need an <b>AI客服</b>?</p>",
                       "account": {"acct": "a", "url": "https://mastodon.social/@a"}, "favourites_count": 2}])
    monkeypatch.delenv("MASTODON_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(a, "_get", fake)
    rows = a.fetch_mastodon("AI 客服")
    assert calls[0].endswith("/api/v1/timelines/tag/AI%E5%AE%A2%E6%9C%8D")
    assert rows[0].text == "Need an AI客服?" and rows[0].metrics == {"like": 2}


def test_mastodon_token_uses_search(monkeypatch):
    calls = []

    def fake(url, **kw):
        calls.append((url, kw["headers"]["Authorization"]))
        return Reply({"statuses": []})
    monkeypatch.setenv("MASTODON_ACCESS_TOKEN", "tok")
    monkeypatch.setattr(a, "_get", fake)
    assert a.fetch_mastodon("crm") == []
    assert calls == [("https://mastodon.social/api/v2/search", "Bearer tok")]


def test_v2ex(monkeypatch):
    monkeypatch.setattr(a, "_get", lambda url, **kw: Reply({"hits": [{"_source": {
        "id": 1250885, "title": "求推荐客服系统", "content": "预算 5k", "member": "abc", "created": "2026-10-22T03:59:31",
        "replies": 3}}]}))
    (row,) = a.fetch_v2ex("客服系统")
    assert row.url == "https://www.v2ex.com/t/1250885" and row.author_url == "https://www.v2ex.com/member/abc"
    assert row.published_at == "2026-10-22T03:59:31+08:00" and row.metrics == {"comment": 3}


def test_discourse_requires_sites(monkeypatch):
    monkeypatch.delenv("DISCOURSE_SITES", raising=False)
    with pytest.raises(RuntimeError, match="DISCOURSE_SITES"):
        a.fetch_discourse("crm")


def test_discourse_posts(monkeypatch):
    monkeypatch.setenv("DISCOURSE_SITES", "https://forum.example.com/, bad host!")
    monkeypatch.setattr(a, "_get", lambda url, **kw: Reply({
        "topics": [{"id": 7, "title": "CRM?", "slug": "crm"}],
        "posts": [{"topic_id": 7, "post_number": 1, "username": "li", "blurb": "need crm", "like_count": 1},
                  {"topic_id": 7, "post_number": 3, "username": "wu", "blurb": "me too"}]}))
    first, reply = a.fetch_discourse("crm")
    assert first.url == "https://forum.example.com/t/crm/7/1" and first.source == "Discourse·forum.example.com"
    assert reply.source.endswith("评论") and reply.post_url == "https://forum.example.com/t/crm/7"


CCGP_PAGE = """<ul class="vT-srch-result-list-bid">
<li><a href="http://www.ccgp.gov.cn/cggg/dfgg/gkzb/202610/t20261020_1.htm" style="line-height:18px" target="_blank">
  某某市人民医院<font color="red">客服</font>系统采购项目公开招标公告</a>
 <p>采购需求：智能客服系统一套……</p>
 <span>2026.10.20 17:20:11 | 采购人：某某市人民医院 | 代理机构：某某招标公司<br/><strong>公开招标公告</strong> | 浙江</span>
</li>
<li><a href="https://evil.example.com/x.htm">bad</a><p>x</p><span>2026.10.20</span></li>
</ul><div class="vT-srch-result-page-con"></div>"""


def test_ccgp_parser(monkeypatch):
    monkeypatch.setattr(a, "_get", lambda url, **kw: Reply(content=CCGP_PAGE.encode("utf-8")))
    (row,) = a.fetch_ccgp("客服")
    assert row.source == "政府采购" and row.title == "某某市人民医院客服系统采购项目公开招标公告"
    assert row.author == "某某市人民医院" and row.published_at == "2026-10-20T17:20:00+08:00"
    assert row.url.startswith("http://www.ccgp.gov.cn/cggg/") and "智能客服系统" in row.text


def test_ccgp_rate_limited(monkeypatch):
    monkeypatch.setattr(a, "_get", lambda url, **kw: Reply(content="您的访问过于频繁,请稍后再试".encode()))
    with pytest.raises(RuntimeError, match="频繁"):
        a.fetch_ccgp("客服")


def test_bluesky_falls_back_between_hosts(monkeypatch):
    hosts = []

    def fake(url, **kw):
        hosts.append(url.split("/")[2])
        if len(hosts) == 1:
            raise http_error(403)
        return Reply({"posts": []})
    monkeypatch.setattr(a, "_get", fake)
    assert a.fetch_bluesky("crm") == [] and hosts == ["api.bsky.app", "public.api.bsky.app"]


def test_reddit_anonymous_403_explains_oauth(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.setattr(a, "_get", lambda *x, **k: (_ for _ in ()).throw(http_error(403)))
    with pytest.raises(RuntimeError, match="REDDIT_CLIENT_ID"):
        a.fetch_reddit_json("crm")


def test_reddit_oauth(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    a._REDDIT_TOKEN.update(value="", expires=0)
    posted = []

    class Token:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"access_token": "T", "expires_in": 3600}
    monkeypatch.setattr(a.requests, "post", lambda url, **kw: posted.append((url, kw["auth"])) or Token())
    got = []

    def fake(url, **kw):
        got.append((url, kw["headers"]["Authorization"]))
        return Reply({"data": {"children": [{"data": {"permalink": "/r/x/comments/1/t/", "title": "need crm",
                                                      "author": "joe_1", "score": 2}}]}})
    monkeypatch.setattr(a, "_get", fake)
    rows = a.fetch_reddit_json("crm")
    a.fetch_reddit_json("crm")
    assert posted == [("https://www.reddit.com/api/v1/access_token", ("id", "secret"))]  # token cached
    assert got[0] == ("https://oauth.reddit.com/search", "Bearer T")
    assert rows[0].url == "https://www.reddit.com/r/x/comments/1/t/"
    a._REDDIT_TOKEN.update(value="", expires=0)


# ---------------------------------------------------------------- intent
@pytest.mark.parametrize("text,intent,lead_type", [
    ("求推荐靠谱的 AI 客服，预算 2 万", "high", "buyer"),
    ("有人能做小程序吗", "high", "buyer"),
    ("Looking for a CRM, any recommendations?", "high", "buyer"),
    ("专业承接 AI 客服定制，欢迎咨询，加微信 abc", "low", "seller"),
    ("招聘客服主管，薪资面议", "low", "seller"),
    ("We offer AI agents. Book a demo today", "low", "seller"),
    ("这个工具好用吗？", "medium", "question"),
    ("今天学习了，没有购买需求", "low", "other"),
])
def test_intent_buyer_vs_seller(text, intent, lead_type):
    r = analyze(text)
    assert (r["intent"], r["lead_type"]) == (intent, lead_type), r


def test_intent_reasons_custom_words_and_recency():
    r = analyze("求推荐", published_at=datetime.now(timezone.utc).isoformat())
    assert "求推荐" in r["reasons"] and "3 天内发布" in r["reasons"] and r["score"] == 85
    assert analyze("想搞一个私域系统", custom_buyer=["私域系统"])["intent"] == "high"
    assert analyze("全网最低价 私域系统", custom_seller=["全网最低价"])["lead_type"] == "seller"
    assert analyze("dd", is_comment=True)["intent"] == "high"
    assert analyze("dd")["intent"] == "low"


def test_parse_cn_time():
    now = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # 12:00 Beijing
    assert parse_cn_time("3小时前", now) == "2026-10-27T09:00:00+08:00"
    assert parse_cn_time("昨天 08:30", now) == "2026-10-26T08:30:00+08:00"
    assert parse_cn_time("编辑于 2026-10-03 · IP 北京", now) == "2026-10-03T00:00:00+08:00"
    assert parse_cn_time("12-30", now) == "2025-12-30T00:00:00+08:00"
    assert parse_cn_time("1.2万", now) == "" and parse_cn_time("", now) == ""


# ---------------------------------------------------------------- app
def _item(text, author="u", url=None, source="V2EX"):
    return PublicItem(source, text[:20], text, url or "https://www.v2ex.com/t/" + str(abs(hash(text)) % 10**8), author)


def test_save_filters_blocklist_excludes_relevance(context):
    m, client = context
    client.put("/api/settings", json={"blocked_authors": ["spam"], "exclude_words": ["招聘"]})
    rows = [_item("求推荐 CRM 系统", "spam"), _item("CRM 招聘销售"), _item("完全无关的内容"), _item("求推荐 CRM 工具")]
    matched, inserted = m.save_items(rows, manual_query="CRM")
    assert inserted == 1
    assert m.save_items([_item("无关但排除词 abc")], manual_query="CRM", excludes=["abc"]) == (0, 0)


def test_hide_sellers_setting(context):
    m, client = context
    client.put("/api/settings", json={"hide_sellers": True})
    assert m.save_items([_item("CRM 专业承接，欢迎咨询")], manual_query="CRM")[1] == 0
    assert m.save_items([_item("求推荐 CRM")], manual_query="CRM")[1] == 1


def test_settings_rescore_existing(context):
    m, client = context
    _, _, ids = m.save_items([_item("想要一个私域 CRM")], manual_query="CRM", return_ids=True)
    assert client.get(f"/api/items/{ids[0]}").json()["intent"] != "high"
    out = client.put("/api/settings", json={"buyer_words": ["私域"]}).json()
    assert out["rescored"] >= 1 and out["buyer_words"] == ["私域"]
    item = client.get(f"/api/items/{ids[0]}").json()
    assert item["intent"] == "high" and "私域" in item["reasons"] and item["lead_type"] == "buyer"


def test_bulk_follow_up_tags_and_filters(context):
    m, client = context
    _, _, ids = m.save_items([_item("求推荐 CRM a"), _item("求推荐 CRM b"), _item("求推荐 CRM c")],
                             manual_query="CRM", return_ids=True)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    r = client.post("/api/items/bulk", json={"ids": ids[:2], "status": "contacted", "follow_up_at": past, "add_tag": "重点"})
    assert r.json()["updated"] == 2
    one = client.get(f"/api/items/{ids[0]}").json()
    assert one["status"] == "contacted" and one["contacted_at"] and one["tags"] == ["重点"]
    due = client.get("/api/items", params={"follow_up": "due"}).json()
    assert sorted(x["id"] for x in due) == sorted(ids[:2])
    assert len(client.get("/api/items", params={"tag": "重点"}).json()) == 2
    assert client.get("/api/items/count", params={"status": "contacted"}).json()["count"] == 2
    assert client.get("/api/overview").json()["follow_up_due"] == 2
    # finishing a lead clears its reminder
    client.patch(f"/api/items/{ids[0]}", json={"status": "won"})
    assert client.get(f"/api/items/{ids[0]}").json()["follow_up_at"] == ""
    assert client.patch(f"/api/items/{ids[1]}", json={"follow_up_at": "2026-11-01T09:00"}).json()["follow_up_at"] == "2026-11-01T01:00:00+00:00"
    assert client.patch(f"/api/items/{ids[1]}", json={"follow_up_at": "next week"}).status_code == 422
    client.post("/api/items/bulk", json={"ids": [ids[2]], "delete": True})
    assert client.get(f"/api/items/{ids[2]}").status_code == 404


def test_block_author_endpoint(context):
    m, client = context
    _, _, ids = m.save_items([_item("CRM 广告 1", "rival"), _item("CRM 广告 2", "rival")], manual_query="CRM", return_ids=True)
    r = client.post("/api/settings/block-author", params={"item_id": ids[0]}).json()
    assert r == {"blocked": "rival", "ignored": 2}
    assert m.save_items([_item("CRM 广告 3", "rival")], manual_query="CRM")[1] == 0


def test_sort_and_lead_type_filter(context):
    m, client = context
    m.save_items([_item("CRM 专业承接 欢迎咨询"), _item("求推荐 CRM")], manual_query="CRM")
    newest = client.get("/api/items", params={"sort": "newest"}).json()
    assert newest[0]["id"] > newest[-1]["id"]
    assert all(x["lead_type"] != "seller" for x in client.get("/api/items", params={"lead_type": "not_seller"}).json())
    assert [x["lead_type"] for x in client.get("/api/items", params={"lead_type": "seller"}).json()] == ["seller"]


def test_export_respects_filters(context):
    m, client = context
    m.save_items([_item("求推荐 CRM x"), _item("CRM 专业承接 欢迎咨询")], manual_query="CRM")
    body = client.get("/api/export.csv", params={"intent": "high", "source": "V2EX"}).content.decode("utf-8-sig")
    lines = body.strip().splitlines()
    assert lines[0].startswith("id,source,title") and "lead_type" in lines[0] and len(lines) == 2


def test_templates_seed_and_crud(context):
    _, client = context
    seeded = client.get("/api/templates").json()
    assert len(seeded) == 3 and "{author}" in seeded[0]["text"]
    t = client.post("/api/templates", json={"name": "短", "text": "hi {author}"}).json()
    assert client.put(f"/api/templates/{t['id']}", json={"name": "短2", "text": "hey"}).json()["name"] == "短2"
    assert client.delete(f"/api/templates/{t['id']}").json()["ok"]
    for x in seeded:
        client.delete(f"/api/templates/{x['id']}")
    assert client.get("/api/templates").json() == []  # deleting all doesn't re-seed


def test_overview(context):
    m, client = context
    m.save_items([_item("求推荐 CRM 1")], manual_query="CRM")
    o = client.get("/api/overview?days=7").json()
    assert o["today_new"] == 1 and o["today_high"] == 1 and o["high_open"] == 1
    assert len(o["daily"]) == 7 and o["daily"][-1]["count"] == 1 and o["platforms"] == [{"platform": "V2EX", "count": 1}]
    assert o["total"] == 1  # demo rows are not counted


def test_monitor_edit_and_excludes(context):
    m, client = context
    mon = client.post("/api/monitors", json={"query": "CRM", "platforms": ["hackernews"], "excludes": ["招聘"]}).json()
    assert mon["excludes"] == ["招聘"]
    r = client.patch(f"/api/monitors/{mon['id']}", json={"query": "AI客服", "interval_minutes": 60,
                                                        "platforms": ["v2ex", "hackernews"], "excludes": []}).json()
    assert (r["query"], r["interval_minutes"], r["platforms"], r["excludes"], r["active"]) == \
        ("AI客服", 60, ["v2ex", "hackernews"], [], True)
    assert client.patch(f"/api/monitors/{mon['id']}", json={"platforms": ["nope"]}).status_code == 422
    assert client.patch(f"/api/monitors/{mon['id']}", json={"active": False}).json()["last_status"] == "paused"


def test_new_sources_in_catalog_and_online_search(context, monkeypatch):
    m, client = context
    ids = {r["id"]: r for r in client.get("/api/monitor-sources").json()}
    for key in ("v2ex", "ccgp", "stackexchange", "lemmy", "mastodon", "discourse", "authorized_mastodon"):
        assert key in ids, key
    assert ids["v2ex"]["configured"] and not ids["discourse"]["configured"]
    monkeypatch.setattr(m, "fetch_v2ex", lambda q: [_item("求推荐 AI客服 系统")])
    monkeypatch.setattr(m, "fetch_ccgp", lambda q: [PublicItem("政府采购", "XX 医院 AI客服 采购公告", "公开招标",
                                                              "http://www.ccgp.gov.cn/a.htm", "XX 医院")])
    out = client.post("/api/search-online", json={"query": "AI客服", "sources": ["v2ex", "ccgp"]}).json()
    assert [r["inserted"] for r in out["reports"]] == [1, 1]
    gov = next(x for x in out["items"] if x["source"] == "政府采购")
    assert gov["intent"] == "high" and gov["score"] >= 90 and gov["lead_type"] == "buyer"


@pytest.mark.parametrize("url,key", [
    ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "msgtype"),
    ("https://oapi.dingtalk.com/robot/send?access_token=x", "msgtype"),
    ("https://open.feishu.cn/open-apis/bot/v2/hook/x", "msg_type"),
    ("https://sctapi.ftqq.com/KEY.send", "desp"),
    ("https://hooks.slack.com/services/x", "text"),
    ("https://example.com/hook", "leads"),
])
def test_alert_formats(context, url, key):
    m, _ = context
    lead = {"source": "V2EX", "title": "求推荐", "text": "", "author": "a", "url": "https://v2ex.com/t/1",
            "score": 90, "reasons": ["求推荐"]}
    payload = m.alert_payload(url, [lead])
    assert key in payload and "求推荐" in str(payload)


def test_high_intent_triggers_webhook(context, monkeypatch):
    m, _ = context
    sent = []
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x")
    monkeypatch.setattr(m.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    monkeypatch.setattr(m.requests, "post", lambda url, json, timeout: sent.append(json))
    m.save_items([_item("求推荐 CRM 9"), _item("CRM 讨论帖")], manual_query="CRM")
    assert len(sent) == 1 and "1 条高意向线索" in sent[0]["text"]["content"]


def test_launcher_build_list_matches_server():
    import app.main as main
    ps1 = (ROOT / "start.ps1").read_text(encoding="utf-8-sig")
    files = re.search(r"\$files = (.+)", ps1).group(1)
    assert [f.replace("\\", "/") for f in re.findall(r"'([^']+)'", files)] == list(main.BUILD_FILES)
