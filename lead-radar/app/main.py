"""Lead Radar: standalone public-content monitoring and manual lead workflow."""
import asyncio
import base64
import hashlib
import html
import importlib.util
import binascii
import secrets
import collections
import csv
import io
import ipaddress
import json
import os
import re
import socket
import threading
import uuid
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Column, Integer, String, Text, create_engine, delete, event, func, or_, select
from sqlalchemy.orm import declarative_base, sessionmaker

from .intent import analyze
from .browser_collector import (
    SUPPORTED as BROWSER_PLATFORMS, LABEL_TO_PLATFORM, canonical_post_url, collect_in_browser,
    locate_visible_comment, open_login_pages, safe_post_url, saved_login_states, browser_channel, BROWSER_NAMES,
)
from .adapters import (
    PLATFORM_ENV, PublicItem, demo_items, fetch_github, fetch_hackernews, fetch_platform,
    fetch_google_news, fetch_bing_news, fetch_bluesky, fetch_reddit_with_comments, fetch_github_with_comments,
    fetch_reddit_json, fetch_rss, fetch_x, fetch_youtube, item_key,
    fetch_stackexchange, fetch_lemmy, fetch_mastodon, fetch_v2ex, fetch_discourse, fetch_ccgp,
)

ROOT = Path(__file__).resolve().parent.parent
# Fingerprint of the running code. start.ps1 computes the same hash over the same files
# to tell whether the server on the port is this exact copy or an older one left running.
BUILD_FILES = ("app/main.py", "app/browser_collector.py", "app/adapters.py", "app/netcheck.py", "app/relay.py",
               "app/intent.py", "static/index.html")
BUILD_ID = hashlib.sha256(b"".join((ROOT / f).read_bytes() for f in BUILD_FILES if (ROOT / f).exists())).hexdigest()[:16]
load_dotenv(ROOT / ".env", override=False)
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(ROOT / "data"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/lead_radar.db")
if DATABASE_URL.startswith("sqlite:///./"):
    DATABASE_URL = "sqlite:///" + str(ROOT / DATABASE_URL.removeprefix("sqlite:///./"))
if not (DATABASE_URL.startswith("sqlite:///") or DATABASE_URL.startswith("postgresql+psycopg://") or DATABASE_URL.startswith("postgresql://")):
    raise RuntimeError("DATABASE_URL 只支持 SQLite 或 PostgreSQL")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
IS_SQLITE = DATABASE_URL.startswith("sqlite:///")
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 20} if IS_SQLITE else {})
if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def sqlite_pragmas(conn, record):
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout=20000")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()
Session = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()
NOW = lambda: datetime.now(timezone.utc).isoformat()
STATUSES = {"new", "saved", "contacted", "replied", "won", "ignored"}


class Keyword(Base):
    __tablename__ = "keywords"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    terms = Column(Text, nullable=False)
    excludes = Column(Text, nullable=False, default="[]")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(String(40))


class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    external_id = Column(String(64), unique=True)
    source = Column(String(120))
    title = Column(Text)
    text = Column(Text)
    url = Column(Text)
    post_url = Column(Text, default="")
    comment_id = Column(String(100), default="")
    comment_url = Column(Text, default="")
    navigation_kind = Column(String(20), default="")
    author = Column(String(300))
    author_url = Column(Text, default="")
    metrics = Column(Text, default="{}")
    metrics_updated_at = Column(String(40), default="")
    published_at = Column(String(80))
    matched_terms = Column(Text)
    intent = Column(String(20))
    score = Column(Integer)
    status = Column(String(30), default="new")
    notes = Column(Text, default="")
    created_at = Column(String(40))
    sentiment = Column(String(20), default="neutral")
    topics = Column(Text, default="[]")
    hashtags = Column(Text, default="[]")
    lead_type = Column(String(20), default="")     # buyer / seller / question / other
    reasons = Column(Text, default="[]")           # phrases that produced the score
    follow_up_at = Column(String(40), default="")  # next follow-up reminder (ISO time)
    contacted_at = Column(String(40), default="")
    tags = Column(Text, default="[]")
    updated_at = Column(String(40), default="")


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False, default="null")


class ReplyTemplate(Base):
    __tablename__ = "reply_templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(String(40))


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    key = Column(String(100), primary_key=True)
    value = Column(String(100), nullable=False)


class IngestLog(Base):
    __tablename__ = "ingest_logs"
    id = Column(Integer, primary_key=True)
    source = Column(String(100))
    query = Column(Text)
    fetched = Column(Integer, default=0)
    matched = Column(Integer, default=0)
    inserted = Column(Integer, default=0)
    error = Column(Text, default="")
    finished_at = Column(String(40))


class Monitor(Base):
    __tablename__ = "monitors"
    id = Column(Integer, primary_key=True)
    query = Column(String(100), nullable=False)
    platforms = Column(Text, nullable=False)
    interval_minutes = Column(Integer, nullable=False, default=10)
    max_posts = Column(Integer, nullable=False, default=8)
    max_comments = Column(Integer, nullable=False, default=10)
    sort = Column(String(20), nullable=False, default="latest")
    excludes = Column(Text, default="[]")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(String(40), nullable=False)
    last_run_at = Column(String(40), default="")
    last_success_at = Column(String(40), default="")
    next_run_at = Column(String(40), default="")
    last_status = Column(String(25), default="pending")
    last_error = Column(Text, default="")
    last_fetched = Column(Integer, default=0)
    last_new = Column(Integer, default=0)
    run_count = Column(Integer, default=0)
    last_run_id = Column(Integer, default=0)


class MonitorRun(Base):
    __tablename__ = "monitor_runs"
    id = Column(Integer, primary_key=True)
    monitor_id = Column(Integer, nullable=False, index=True)
    finished_at = Column(String(40), nullable=False)
    fetched = Column(Integer, default=0)
    inserted = Column(Integer, default=0)
    status = Column(String(25), default="")
    details = Column(Text, default="")


class KeywordIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    terms: list[str] = Field(min_length=1, max_length=30)
    excludes: list[str] = Field(default_factory=list, max_length=30)
    active: bool = True


class KeywordPatch(BaseModel):
    name: str | None = None
    terms: list[str] | None = None
    excludes: list[str] | None = None
    active: bool | None = None


class StatusIn(BaseModel):
    status: str | None = None
    notes: str | None = Field(default=None, max_length=10000)
    follow_up_at: str | None = Field(default=None, max_length=40)
    tags: list[str] | None = Field(default=None, max_length=20)


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    status: str | None = None
    follow_up_at: str | None = Field(default=None, max_length=40)
    add_tag: str | None = Field(default=None, max_length=30)
    delete: bool = False


class SettingsIn(BaseModel):
    buyer_words: list[str] = Field(default_factory=list, max_length=200)
    seller_words: list[str] = Field(default_factory=list, max_length=200)
    exclude_words: list[str] = Field(default_factory=list, max_length=200)
    blocked_authors: list[str] = Field(default_factory=list, max_length=500)
    hide_sellers: bool = False
    notify_min_score: int = Field(default=80, ge=0, le=100)
    browser_background: bool = True


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


class IngestIn(BaseModel):
    source: str = "demo"
    query: str | None = None
    url: str | None = None


PUBLIC_SEARCH_SOURCES = ("googlenews", "bingnews", "github", "hackernews", "reddit", "bluesky", "youtube",
                         "v2ex", "stackexchange", "lemmy", "mastodon", "discourse", "ccgp")
# Authorized-JSON platform ids that clash with a built-in public source get this prefix.
AUTHORIZED_PREFIXED = ("bluesky", "hackernews", "mastodon")


class PlatformManualIn(BaseModel):
    # Save only user-selected publicly shareable posts; this is NOT a platform crawler.
    platform: str
    url: str = Field(min_length=10, max_length=2000)
    title: str = Field(default="", max_length=500)
    text: str = Field(min_length=1, max_length=20000)
    author: str = Field(default="", max_length=300)


def validate_platform_url(platform: str, url: str) -> str:
    allowed = {
        "douyin": ("douyin.com", "iesdouyin.com"),
        "xhs": ("xiaohongshu.com", "xhslink.com"),
        "weibo": ("weibo.com",), "zhihu": ("zhihu.com",),
        "bilibili": ("bilibili.com",), "kuaishou": ("kuaishou.com",),
        "tiktok": ("tiktok.com",), "tieba": ("tieba.baidu.com",),
    }
    if platform not in allowed:
        raise HTTPException(422, "不支持该来源的公开内容链接")
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise HTTPException(422, "仅允许 HTTPS 公开内容链接")
    if not any(host == d or host.endswith("." + d) for d in allowed[platform]):
        raise HTTPException(422, "原文链接与选择的平台不一致")
    return url.strip()


class BrowserSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    platforms: list[str] = Field(default_factory=lambda: ["xhs", "douyin"], min_length=1, max_length=8)
    max_posts: int = Field(default=8, ge=1, le=20)
    max_comments: int = Field(default=10, ge=0, le=30)
    sort: Literal["latest", "relevance"] = "latest"


class MonitorIn(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    platforms: list[str] = Field(min_length=1, max_length=40)
    interval_minutes: int = Field(default=10, ge=5, le=1440)
    max_posts: int = Field(default=8, ge=1, le=20)
    max_comments: int = Field(default=10, ge=0, le=30)
    sort: Literal["latest", "relevance"] = "latest"
    excludes: list[str] = Field(default_factory=list, max_length=30)


class MonitorPatch(BaseModel):
    active: bool | None = None
    query: str | None = Field(default=None, min_length=1, max_length=100)
    platforms: list[str] | None = Field(default=None, min_length=1, max_length=40)
    interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    max_posts: int | None = Field(default=None, ge=1, le=20)
    max_comments: int | None = Field(default=None, ge=0, le=30)
    sort: Literal["latest", "relevance"] | None = None
    excludes: list[str] | None = Field(default=None, max_length=30)


class OnlineSearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    sources: list[str] = Field(default_factory=lambda: ["googlenews", "bingnews", "github", "hackernews"], min_length=1,
                               max_length=len(PUBLIC_SEARCH_SOURCES))


def _json_dict(raw):
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


def json_list(raw):
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []


def clean_words(words):
    cleaned = list(dict.fromkeys(w.strip() for w in words if isinstance(w, str) and w.strip()))
    if len(cleaned) > 30 or any(len(w) > 100 for w in cleaned):
        raise HTTPException(422, "关键词数量或长度超限")
    return cleaned


def classify(text, **kwargs):
    """(intent, score). Transparent rule-based priority estimate, see app/intent.py."""
    result = analyze(text, **kwargs)
    return result["intent"], result["score"]


SETTING_DEFAULTS = {"buyer_words": [], "seller_words": [], "exclude_words": [], "blocked_authors": [],
                    "hide_sellers": False, "notify_min_score": 80, "browser_background": True}


def get_settings(session=None):
    def load(s):
        rows = {r.key: r.value for r in s.scalars(select(Setting)).all()}
        out = dict(SETTING_DEFAULTS)
        for key in out:
            if key in rows:
                try:
                    out[key] = json.loads(rows[key])
                except ValueError:
                    pass
        return out
    if session is not None:
        return load(session)
    with Session() as s:
        return load(s)


def analyze_item(source, title, text, published_at="", settings=None):
    settings = settings or SETTING_DEFAULTS
    result = analyze((title or "") + " " + (text or ""), is_comment=(source or "").endswith("评论"),
                     published_at=published_at or "", custom_buyer=settings.get("buyer_words") or (),
                     custom_seller=settings.get("seller_words") or ())
    if source in ("Google News", "Bing News"):
        # News reports are not customer purchase requests.
        result.update(intent="low", score=0, lead_type="other", reasons=["新闻资讯，不是客户需求"])
    elif source == "政府采购":
        # A public procurement notice is an organisation announcing it will buy.
        result.update(intent="high", score=max(result["score"], 90), lead_type="buyer",
                      reasons=["政府/单位采购公告"] + [r for r in result["reasons"] if not r.startswith("疑似商家")])
    return result


def enrich(text):
    t = (text or "").casefold()
    pos = ["好用", "推荐", "满意", "成功", "方便", "great", "love", "helpful", "solved"]
    neg = ["差", "失败", "问题", "麻烦", "不行", "坑", "bad", "broken", "frustrated"]
    p, n = sum(w in t for w in pos), sum(w in t for w in neg)
    sentiment = "positive" if p > n else "negative" if n > p else "neutral"
    hashtags = sorted(set(re.findall(r"[#＃]([\w\u4e00-\u9fff-]+)", t)))[:30]
    words = re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,8}", t)
    stop = {"公司", "一个", "没有", "我们", "这个", "想要", "有没有", "正在"}
    topics = [w for w, _ in collections.Counter(w for w in words if w not in stop).most_common(5)]
    return sentiment, topics, hashtags


def init_db():
    # SQLite/PostgreSQL additive migration; preserve saved leads, notes and IDs.
    Base.metadata.create_all(engine)
    from sqlalchemy import inspect
    column_names = {c["name"] for c in inspect(engine).get_columns("items")}
    columns_to_add = {
        "sentiment": "TEXT DEFAULT 'neutral'", "topics": "TEXT DEFAULT '[]'",
        "hashtags": "TEXT DEFAULT '[]'", "post_url": "TEXT DEFAULT ''",
        "comment_id": "VARCHAR(100) DEFAULT ''", "comment_url": "TEXT DEFAULT ''",
        "navigation_kind": "VARCHAR(20) DEFAULT ''", "author_url": "TEXT DEFAULT ''",
        "metrics": "TEXT DEFAULT '{}'", "metrics_updated_at": "VARCHAR(40) DEFAULT ''",
        "lead_type": "VARCHAR(20) DEFAULT ''", "reasons": "TEXT DEFAULT '[]'",
        "follow_up_at": "VARCHAR(40) DEFAULT ''", "contacted_at": "VARCHAR(40) DEFAULT ''",
        "tags": "TEXT DEFAULT '[]'", "updated_at": "VARCHAR(40) DEFAULT ''",
    }
    monitor_columns = {c["name"] for c in inspect(engine).get_columns("monitors")}
    with engine.begin() as con:
        for col, decl in columns_to_add.items():
            if col not in column_names:
                con.exec_driver_sql(f"ALTER TABLE items ADD COLUMN {col} {decl}")
        if "sort" not in monitor_columns:
            con.exec_driver_sql("ALTER TABLE monitors ADD COLUMN sort VARCHAR(20) DEFAULT 'latest' NOT NULL")
        if "excludes" not in monitor_columns:
            con.exec_driver_sql("ALTER TABLE monitors ADD COLUMN excludes TEXT DEFAULT '[]'")
    _migrate_browser_dedup_keys()
    _migrate_unescape_html()
    # Repair misleading v3 fake fragments without touching the stable dedup key.
    with Session.begin() as s:
        for old in s.scalars(select(Item).where(Item.url.like('%#lr-comment-%'))).all():
            if (old.source or "").endswith("评论"):
                base = old.url.split("#lr-comment-", 1)[0]
                old.post_url, old.url, old.navigation_kind = base, base, "assisted"
    with Session.begin() as s:
        version = s.get(SchemaMeta, "rule_score_version")
        if version is None or version.value != SCORE_VERSION:
            settings = get_settings(s)
            for old_item in s.scalars(select(Item)).all():
                _apply_analysis(old_item, analyze_item(old_item.source, old_item.title, old_item.text,
                                                       old_item.published_at, settings))
            if version is None:
                s.add(SchemaMeta(key="rule_score_version", value=SCORE_VERSION))
            else:
                version.value = SCORE_VERSION
    with Session.begin() as s:
        if not s.scalar(select(func.count()).select_from(Keyword)):
            s.add(Keyword(name="AI 获客", terms=json.dumps(["GPT", "AI客服", "AI 客服", "自动化获客"], ensure_ascii=False),
                          excludes=json.dumps(["招聘"], ensure_ascii=False), active=True, created_at=NOW()))
    if os.getenv("SEED_DEMO", "0") == "1":
        with Session() as s:
            if not s.scalar(select(func.count()).select_from(Item)):
                save_items(demo_items())


SCORE_VERSION = "4.0.1"


def _apply_analysis(item, result):
    item.intent, item.score, item.lead_type = result["intent"], result["score"], result["lead_type"]
    item.reasons = json.dumps(result["reasons"], ensure_ascii=False)


def rescore_all():
    with Session.begin() as s:
        settings = get_settings(s)
        rows = s.scalars(select(Item)).all()
        for item in rows:
            _apply_analysis(item, analyze_item(item.source, item.title, item.text, item.published_at, settings))
        return len(rows)


def _browser_dedup_key(source: str, url: str, post_url: str, author: str, text: str):
    """3.3 key: canonical work URL (no xsec_token / query / locale) so re-runs don't duplicate."""
    for suffix in ("作品", "评论"):
        if source.endswith(suffix) and source[:-len(suffix)] in LABEL_TO_PLATFORM:
            platform = LABEL_TO_PLATFORM[source[:-len(suffix)]]
            base = post_url or url.split("#", 1)[0]
            if not safe_post_url(platform, base):
                return None
            canonical = canonical_post_url(platform, base)
            if suffix == "作品":
                return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            marker = hashlib.sha256(((author or "") + "\0" + (text or "")).encode("utf-8")).hexdigest()[:20]
            return hashlib.sha256((canonical + "#lr-comment-" + marker).encode("utf-8")).hexdigest()
    return None


def _migrate_browser_dedup_keys():
    with Session.begin() as s:
        version = s.get(SchemaMeta, "dedup_version")
        if version is not None and version.value == "3.3":
            return
        labels = [label + suffix for label in BROWSER_PLATFORMS.values() for suffix in ("作品", "评论")]
        used = set(s.scalars(select(Item.external_id)).all())
        for item in s.scalars(select(Item).where(Item.source.in_(labels))).all():
            key = _browser_dedup_key(item.source or "", item.url or "", item.post_url or "", item.author or "", item.text or "")
            if key and key != item.external_id and key not in used:
                used.discard(item.external_id)
                item.external_id = key
                used.add(key)
        if version is None:
            s.add(SchemaMeta(key="dedup_version", value="3.3"))
        else:
            version.value = "3.3"


def _migrate_unescape_html():
    """Before 3.3, Hacker News/RSS text was stored with HTML entities (&#x27; &#x2F;)."""
    with Session.begin() as s:
        if s.get(SchemaMeta, "html_unescape") is not None:
            return
        for item in s.scalars(select(Item).where(
                Item.source.in_(("Hacker News", "Hacker News评论", "RSS", "Google News", "Bing News")),
                or_(Item.text.like("%&#%"), Item.text.like("%&quot;%"), Item.text.like("%&amp;%"),
                    Item.title.like("%&#%")))).all():
            item.text = html.unescape(item.text or "")
            item.title = html.unescape(item.title or "")
        s.add(SchemaMeta(key="html_unescape", value="3.3"))


def active_keywords(session):
    return [(json_list(k.terms), json_list(k.excludes)) for k in session.scalars(select(Keyword).where(Keyword.active.is_(True))).all()]


BROWSER_WORK_SOURCES = {label + "作品" for label in BROWSER_PLATFORMS.values()}


def _norm(text):
    return re.sub(r"\s+", "", (text or "").casefold())


def _relevant(item, query):
    """Public APIs often return loosely related hits (Lemmy/HN match URLs, Bluesky stems words).
    Keep a post only when one of the query words really appears in it. Comments are kept: they
    were collected *because* their post matched. Browser search pages are trusted as-is."""
    if (item.source or "").endswith("评论") or item.source in BROWSER_WORK_SOURCES or item.source in ("政府采购", "Demo"):
        return True
    hay = _norm((item.title or "") + " " + (item.text or ""))
    # Every word must appear ("求推荐 CRM" is not satisfied by "求推荐耳塞"), like a search engine AND.
    words = [_norm(w) for w in re.split(r"[\s,，、|]+", query or "") if _norm(w)]
    return not words or all(w in hay for w in words)


def save_items(data, *, manual_query="", return_ids=False, excludes=()):
    """Return matched and new insert counts; preserve existing notes/status."""
    matched, inserted = 0, 0
    saved_ids = []
    new_high = []
    with Session.begin() as s:
        groups = active_keywords(s)
        settings = get_settings(s)
        blocked = {_norm(x) for x in settings["blocked_authors"] if _norm(x)}
        drop_words = [w.casefold() for w in list(settings["exclude_words"]) + list(excludes or ()) if w and w.strip()]
        for item in data:
            hay = ((item.title or "") + " " + (item.text or "")).casefold()
            if blocked and (_norm(item.author) in blocked or _norm(item.author_url) in blocked):
                continue
            if drop_words and any(w in hay for w in drop_words):
                continue
            if manual_query and not _relevant(item, manual_query):
                continue
            terms_found = set()
            for terms, kw_excludes in groups:
                if any(w.casefold() in hay for w in kw_excludes):
                    continue
                terms_found.update(w for w in terms if w.casefold() in hay)
            # Manual online search is independent of stored keyword groups.
            # Previously this gate silently discarded most valid online matches.
            if manual_query:
                terms_found.add(manual_query.strip())
            if not terms_found:
                continue
            matched += 1
            key = item_key(item)
            old_id = s.scalar(select(Item.id).where(Item.external_id == key))
            if old_id is not None:
                if item.metrics or item.author_url:
                    existing = s.get(Item, old_id)
                    if existing is not None:
                        if item.metrics:  # counters change over time; keep the latest reading
                            merged = {**_json_dict(existing.metrics), **item.metrics}
                            existing.metrics = json.dumps(merged, ensure_ascii=False)
                            existing.metrics_updated_at = NOW()
                        if item.author_url and not existing.author_url:
                            existing.author_url = item.author_url[:600]
                if item.navigation_kind:
                    existing = s.get(Item, old_id)
                    if existing and (not existing.navigation_kind or existing.navigation_kind in ("assisted", "manual") or
                                     (item.navigation_kind == "direct" and not existing.comment_url)):
                        existing.post_url = (item.post_url or "")[:2000]
                        existing.comment_id = (item.comment_id or "")[:100]
                        existing.comment_url = (item.comment_url or "")[:2000]
                        existing.navigation_kind = item.navigation_kind
                        existing.url = (item.url or "")[:2000]
                saved_ids.append(old_id)
                continue
            result = analyze_item(item.source, item.title, item.text, item.published_at, settings)
            if settings["hide_sellers"] and result["lead_type"] == "seller":
                continue
            intent, score = result["intent"], result["score"]
            sentiment, topics, hashtags = enrich(hay)
            obj = Item(external_id=key, source=item.source, title=(item.title or "")[:1000], text=(item.text or "")[:20000],
                       url=(item.url or "")[:2000], post_url=(item.post_url or "")[:2000],
                       comment_id=(item.comment_id or "")[:100], comment_url=(item.comment_url or "")[:2000],
                       navigation_kind=(item.navigation_kind or
                           ("direct" if item.source in ("Reddit评论", "GitHub评论", "YouTube评论", "Hacker News评论") else "")), author=(item.author or "")[:300], published_at=item.published_at,
                       author_url=(item.author_url or "")[:600], metrics=json.dumps(item.metrics or {}, ensure_ascii=False),
                       metrics_updated_at=NOW() if item.metrics else "",
                       matched_terms=json.dumps(sorted(terms_found), ensure_ascii=False), intent=intent, score=score,
                       lead_type=result["lead_type"], reasons=json.dumps(result["reasons"], ensure_ascii=False),
                       status="new", notes="", created_at=NOW(), updated_at=NOW(), sentiment=sentiment,
                       topics=json.dumps(topics, ensure_ascii=False), hashtags=json.dumps(hashtags, ensure_ascii=False))
            s.add(obj)
            s.flush()
            saved_ids.append(obj.id)
            inserted += 1
            if intent == "high" and score >= settings["notify_min_score"] and item.source != "Demo":
                new_high.append({"source": item.source, "title": (item.title or "")[:200], "text": (item.text or "")[:200],
                                 "author": item.author or "", "url": item.comment_url or item.url, "score": score,
                                 "reasons": result["reasons"][:4]})
    if new_high:
        # Never block or lose collected data because the optional notification service is slow or down.
        threading.Thread(target=send_alert, args=(new_high,), daemon=True).start()
    return (matched, inserted, saved_ids) if return_ids else (matched, inserted)


def _alert_text(leads):
    lines = [f"Lead Radar 发现 {len(leads)} 条高意向线索"]
    for x in leads[:10]:
        lines.append(f"\n【{x['source']}】{x['score']}分 {x.get('author') or ''}\n{(x.get('title') or x.get('text') or '')[:120]}"
                     f"\n原因：{'、'.join(x.get('reasons') or [])}\n{x['url']}")
    return "\n".join(lines)


def alert_payload(url, leads):
    """Webhook body in the format the receiving service expects (企业微信/钉钉/飞书/Server酱/Slack/Discord)."""
    host = (urlparse(url).hostname or "").lower()
    text = _alert_text(leads)
    if host == "qyapi.weixin.qq.com":
        return {"msgtype": "text", "text": {"content": text[:2000]}}
    if host == "oapi.dingtalk.com":
        return {"msgtype": "text", "text": {"content": text[:2000]}}
    if host.endswith("feishu.cn") or host.endswith("larksuite.com"):
        return {"msg_type": "text", "content": {"text": text[:4000]}}
    if host.endswith("ftqq.com"):
        return {"title": f"Lead Radar：{len(leads)} 条高意向线索", "desp": text.replace("\n", "\n\n")[:30000]}
    if host == "hooks.slack.com":
        return {"text": text[:3000]}
    if host in ("discord.com", "discordapp.com"):
        return {"content": text[:1900]}
    return {"event": "new_high_intent_leads", "count": len(leads), "leads": leads[:20], "text": text}


def send_alert(leads, url=None):
    url = url or os.getenv("ALERT_WEBHOOK_URL", "")
    if not leads or urlparse(url).scheme != "https":
        return None
    try:
        return requests.post(url, json=alert_payload(url, leads), timeout=10)
    except requests.RequestException:
        return None


def log_ingest(source, query, fetched=0, matched=0, inserted=0, error=""):
    with Session.begin() as s:
        s.add(IngestLog(source=source, query=query[:500], fetched=fetched, matched=matched, inserted=inserted,
                        error=error[:1000], finished_at=NOW()))
        # Bound log history without growing the database forever.
        ids = s.scalars(select(IngestLog.id).order_by(IngestLog.id.desc()).offset(500)).all()
        if ids:
            s.execute(delete(IngestLog).where(IngestLog.id.in_(ids)))


def _validate_rss_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(400, "RSS 地址必须是有效的公开 HTTP(S) URL")
    # Do not allow user-provided links to local/cloud-metadata/internal services.
    try:
        addresses = socket.getaddrinfo(parsed.hostname, None)
    except OSError as exc:
        raise HTTPException(400, "无法解析 RSS 主机地址") from exc
    for row in addresses:
        addr = ipaddress.ip_address(row[4][0])
        if not addr.is_global:
            raise HTTPException(400, "RSS 地址不能指向内网或本机")
    return url


def available_sources():
    sources = [
        {"id": "googlenews", "name": "Google News 资讯搜索（非客户名单）", "configured": True, "kind": "public", "note": "公开 RSS；可能受地区和网络限制"},
        {"id": "bingnews", "name": "Bing News 资讯搜索（非客户名单）", "configured": True, "kind": "public", "note": "公开 RSS；可能受地区和网络限制"},
        {"id": "reddit", "name": "Reddit 帖子", "configured": True, "kind": "public",
         "note": ("已配置官方 OAuth" if os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")
                  else "Reddit 已基本禁止未登录访问；在 .env 填 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET（免费）后才稳定")},
        {"id": "github", "name": "GitHub 公开 Issues", "configured": True, "kind": "public", "note": "未配置令牌时有请求限额"},
        {"id": "hackernews", "name": "Hacker News 帖子与匹配评论", "configured": True, "kind": "public"},
        {"id": "bluesky", "name": "Bluesky 公开帖子", "configured": True, "kind": "public", "note": "公开 AppView，部分服务器仍可能要求认证"},
        {"id": "v2ex", "name": "V2EX 帖子（中文技术/创业社区）", "configured": True, "kind": "public", "note": "通过 SOV2EX 公开全文搜索，按发布时间排序"},
        {"id": "ccgp", "name": "中国政府采购网 招标/采购公告", "configured": True, "kind": "public",
         "note": "最近 30 天的公开采购公告（采购人即潜在客户）；网站限制访问频率，监控建议 3 小时以上"},
        {"id": "stackexchange", "name": "Stack Overflow / Stack Exchange 提问", "configured": True, "kind": "public",
         "note": "官方 API，免密钥每天约 300 次；STACKEXCHANGE_SITES 可选站点"},
        {"id": "lemmy", "name": "Lemmy 帖子与评论", "configured": True, "kind": "public", "note": "联邦社区，LEMMY_INSTANCE 可换实例"},
        {"id": "mastodon", "name": "Mastodon 公开嘟文", "configured": True, "kind": "public",
         "note": "未配置 MASTODON_ACCESS_TOKEN 时按话题标签(#关键词)读取"},
        {"id": "discourse", "name": "Discourse 论坛", "configured": bool(os.getenv("DISCOURSE_SITES", "").strip()), "kind": "key",
         "note": "在 .env 的 DISCOURSE_SITES 填写论坛域名（逗号隔开）"},
        {"id": "rss", "name": "RSS / Atom 订阅", "configured": True, "kind": "public"},
        {"id": "demo", "name": "演示数据（不是真实客户）", "configured": True, "kind": "demo"},
        {"id": "youtube", "name": "YouTube 官方 API", "configured": bool(os.getenv("YOUTUBE_API_KEY")), "kind": "key"},
        {"id": "x", "name": "X 官方 API", "configured": bool(os.getenv("X_BEARER_TOKEN")), "kind": "key"},
    ]
    sources += [{"id": "authorized_" + k if k in AUTHORIZED_PREFIXED else k,
                 "name": name + " 授权 JSON 接口", "configured": bool(os.getenv(env)), "kind": "authorized"}
                for k, (name, env, _) in PLATFORM_ENV.items()]
    return sources


def do_ingest(inp):
    source = inp.source.strip().casefold()
    sources = {x["id"]: x for x in available_sources()}
    if source not in sources:
        raise HTTPException(400, "不支持该数据源")
    query = (inp.query or "").strip()
    if source not in ("rss", "demo") and not query:
        raise HTTPException(400, "请先输入采集关键词")
    if len(query) > 300:
        raise HTTPException(400, "查询词过长")
    if source == "rss" and not (inp.url or "").strip():
        raise HTTPException(400, "请填写 RSS / Atom 地址")
    if not sources[source]["configured"]:
        raise HTTPException(400, "数据源尚未配置官方密钥或授权接口")
    try:
        if source == "demo":
            data = demo_items()
        elif source == "rss":
            data = fetch_rss(_validate_rss_url(inp.url.strip()), source="RSS")
        elif source in SEARCH_PROVIDERS:
            data = SEARCH_PROVIDERS[source](query)
        else:
            data = fetch_platform(source.removeprefix("authorized_"), query)
        matched, inserted = save_items(data)
        result = {"source": source, "fetched": len(data), "matched": matched, "inserted": inserted,
                  "duplicates": matched - inserted, "demo": source == "demo"}
        log_ingest(source, query or inp.url or "", **{k: result[k] for k in ("fetched", "matched", "inserted")})
        return result
    except HTTPException:
        raise
    except Exception as exc:
        error = str(exc)[:500]
        log_ingest(source, query or inp.url or "", error=error)
        raise HTTPException(502, "采集失败：" + error) from exc


def browser_available():
    """Browser platforms need Playwright (start.bat installs it); the Docker image has none."""
    return importlib.util.find_spec("playwright") is not None


def monitor_source_catalog():
    """Registry driving both UI and monitoring validation. Nonpublic sources need explicit auth."""
    has_browser = browser_available()
    rows = [{"id": id, "name": name, "mode": "browser", "configured": has_browser,
             "note": ("浏览器读取可见作品、互动数据和评论；首次需在弹出的浏览器里登录，平台可能限制访问"
                      if has_browser else "未安装浏览器组件：请关闭程序后重新双击 start.bat 自动安装（Docker 版不支持浏览器采集）")}
            for id, name in BROWSER_PLATFORMS.items()]
    for source in available_sources():
        if source["id"] in ("rss", "demo"):
            continue
        source_id = "authorized_" + source["id"] if source["id"] in BROWSER_PLATFORMS else source["id"]
        rows.append({"id": source_id, "name": source["name"],
                     "mode": "api", "configured": source["configured"],
                     "note": source.get("note") or ("需要 .env 中的授权或 API 密钥" if not source["configured"] else "可联网请求；结果受权限和配额限制")})
    return rows


# One registry for every public/key API source (single search, online search, monitors).
# Looked up by name at call time so tests can monkeypatch the fetch_* functions.
SEARCH_PROVIDERS = {name: (lambda q, _f=fn: globals()[_f](q)) for name, fn in {
    "googlenews": "fetch_google_news", "bingnews": "fetch_bing_news", "reddit": "fetch_reddit_json",
    "github": "fetch_github", "hackernews": "fetch_hackernews", "bluesky": "fetch_bluesky",
    "youtube": "fetch_youtube", "x": "fetch_x", "v2ex": "fetch_v2ex", "stackexchange": "fetch_stackexchange",
    "lemmy": "fetch_lemmy", "mastodon": "fetch_mastodon", "discourse": "fetch_discourse", "ccgp": "fetch_ccgp",
}.items()}


def monitor_fetch(source: str, query: str, max_posts: int, max_comments: int):
    """Bounded direct public/authorized API polling. Error is surfaced to operator."""
    providers = {
        **SEARCH_PROVIDERS,
        "reddit": lambda q: fetch_reddit_with_comments(q, max_posts=max_posts, max_comments=max_comments),
        "github": lambda q: fetch_github_with_comments(q, max_issues=max_posts, max_comments=max_comments),
        "youtube": lambda q: fetch_youtube(q, include_comments=True,
                                          max_videos=max_posts, max_comments=max_comments),
    }
    if source in providers:
        return providers[source](query)[:max(50, max_posts * (max_comments + 1))]
    if source.startswith("authorized_") and source.removeprefix("authorized_") in PLATFORM_ENV:
        return fetch_platform(source.removeprefix("authorized_"), query)[:200]
    if source in PLATFORM_ENV:
        return fetch_platform(source, query)[:200]
    raise ValueError("未知监控来源")



def default_query():
    with Session() as s:
        kws = active_keywords(s)
    return next((term for terms, _ in kws for term in terms if term), "")


def do_ingest_all(query=""):
    query = query.strip() or default_query()
    if not query:
        raise HTTPException(400, "请先添加至少一个有效关键词")
    results = []
    for source in available_sources():
        if source["kind"] not in ("authorized", "key") or not source["configured"]:
            continue
        try:
            results.append(do_ingest(IngestIn(source=source["id"], query=query)))
        except HTTPException as e:
            results.append({"source": source["id"], "error": e.detail})
    return {"query": query, "results": results, "configured_sources": len(results)}


async def scheduled_sync(seconds):
    while True:
        await asyncio.sleep(seconds)
        query = default_query()
        if not query:
            continue
        for source in available_sources():
            if source["id"] in ("demo", "rss") or not source["configured"]:
                continue
            try:
                await asyncio.to_thread(do_ingest, IngestIn(source=source["id"], query=query))
            except Exception:
                # Error is recorded in /api/ingest-logs; continue with other sources.
                pass


@asynccontextmanager
async def lifespan(app):
    if os.getenv("APP_BIND_ADDRESS", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1") and not os.getenv("APP_PASSWORD"):
        raise RuntimeError("对外绑定端口之前请在 .env 设置 APP_PASSWORD")
    init_db()
    # A previous server process may have died mid-poll. Resume persisted tasks.
    with Session.begin() as session:
        for monitor in session.scalars(select(Monitor).where(Monitor.active.is_(True),
                              Monitor.last_status == "running")).all():
            monitor.last_status = "pending"
            monitor.next_run_at = NOW()
    seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "0"))
    task = asyncio.create_task(scheduled_sync(seconds)) if seconds > 0 else None
    monitor_task = asyncio.create_task(monitor_scheduler())
    try:
        yield
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Lead Radar API", version="4.1.1", lifespan=lifespan)

BROWSER_JOBS = {}
BROWSER_JOB_LOCK = threading.Lock()
BROWSER_EXEC_LOCK = threading.Lock()

@app.get("/api/monitor-sources")
def monitor_sources():
    return monitor_source_catalog()




@app.middleware("http")
async def require_login(request: Request, call_next):
    """Optional browser HTTP Basic password for LAN access; no external auth dependency."""
    password = os.getenv("APP_PASSWORD", "")
    if password and request.url.path != "/api/health":
        credentials = request.headers.get("authorization", "")
        valid = False
        if credentials.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(credentials[6:], validate=True).decode("utf-8")
                username, given = decoded.split(":", 1)
                valid = secrets.compare_digest(username, os.getenv("APP_USERNAME", "admin")) and secrets.compare_digest(given, password)
            except (ValueError, UnicodeError, binascii.Error):
                pass
        if not valid:
            return Response("需要登录", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Lead Radar"'})
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health(request: Request):
    try:
        with Session() as s:
            s.execute(select(func.count()).select_from(Item)).scalar_one()
        out = {"ok": True, "version": "4.1.1", "build": BUILD_ID, "online_search": True,
               "platform_search": "browser-visible", "monitoring": "scheduled-polling"}
        if request.client and request.client.host in ("127.0.0.1", "::1", "localhost", "testclient"):
            out["root"] = str(ROOT)  # local only: lets start.ps1 recognise a copy in another folder
        return out
    except Exception:
        raise HTTPException(503, "数据库暂不可用")


def keyword_dict(k):
    return {"id": k.id, "name": k.name, "terms": json_list(k.terms), "excludes": json_list(k.excludes),
            "active": bool(k.active), "created_at": k.created_at}


@app.get("/api/keywords")
def keywords():
    with Session() as s:
        return [keyword_dict(k) for k in s.scalars(select(Keyword).order_by(Keyword.id.desc()))]


@app.post("/api/keywords", status_code=201)
def add_keyword(k: KeywordIn):
    terms, excludes = clean_words(k.terms), clean_words(k.excludes)
    if not terms:
        raise HTTPException(422, "至少填写一个非空关键词")
    with Session.begin() as s:
        obj = Keyword(name=k.name.strip(), terms=json.dumps(terms, ensure_ascii=False),
                      excludes=json.dumps(excludes, ensure_ascii=False), active=k.active, created_at=NOW())
        s.add(obj)
        s.flush()
        result = keyword_dict(obj)
    return result


@app.patch("/api/keywords/{kid}")
def update_keyword(kid: int, patch: KeywordPatch):
    with Session.begin() as s:
        k = s.get(Keyword, kid)
        if k is None:
            raise HTTPException(404, "关键词组不存在")
        if patch.name is not None:
            if not patch.name.strip() or len(patch.name) > 100:
                raise HTTPException(422, "关键词组名称不能为空或超长")
            k.name = patch.name.strip()
        if patch.terms is not None:
            terms = clean_words(patch.terms)
            if not terms:
                raise HTTPException(422, "至少填写一个关键词")
            k.terms = json.dumps(terms, ensure_ascii=False)
        if patch.excludes is not None:
            k.excludes = json.dumps(clean_words(patch.excludes), ensure_ascii=False)
        if patch.active is not None:
            k.active = patch.active
        s.flush()
        return keyword_dict(k)


@app.delete("/api/keywords/{kid}")
def delete_keyword(kid: int):
    with Session.begin() as s:
        k = s.get(Keyword, kid)
        if k is None:
            raise HTTPException(404, "关键词组不存在")
        s.delete(k)
    return {"ok": True}


def item_dict(item):
    fields = ("id", "source", "title", "text", "url", "post_url", "comment_id", "comment_url", "navigation_kind", "author", "author_url", "published_at", "intent", "score", "status", "notes", "created_at", "sentiment", "metrics_updated_at",
              "lead_type", "follow_up_at", "contacted_at", "updated_at")
    out = {f: getattr(item, f) for f in fields}
    out["metrics"] = _json_dict(item.metrics)
    for f in ("matched_terms", "topics", "hashtags", "reasons", "tags"):
        out[f] = json_list(getattr(item, f))
    return out


def item_query(q="", source="", status="", intent="", author="", sentiment="", platform="", kind="",
               lead_type="", days=0, follow_up="", tag="", sort="score"):
    stmt = select(Item)
    if platform:  # platform display name, e.g. 小红书 / Reddit
        stmt = stmt.where(or_(Item.source == platform, Item.source.like(platform[:60] + "%")))
    if kind == "comment":
        stmt = stmt.where(Item.source.like("%评论"))
    elif kind == "work":
        stmt = stmt.where(Item.source.not_like("%评论"))
    if q:
        pattern = f"%{q[:200]}%"
        stmt = stmt.where(or_(Item.title.ilike(pattern), Item.text.ilike(pattern), Item.author.ilike(pattern),
                              Item.notes.ilike(pattern)))
    if source:
        stmt = stmt.where(Item.source == source)
    if status == "open":  # everything still worth working on
        stmt = stmt.where(Item.status.in_(("new", "saved", "contacted", "replied")))
    elif status:
        stmt = stmt.where(Item.status == status)
    if intent:
        stmt = stmt.where(Item.intent == intent)
    if author:
        stmt = stmt.where(Item.author.ilike(f"%{author[:100]}%"))
    if sentiment:
        stmt = stmt.where(Item.sentiment == sentiment)
    if lead_type == "not_seller":
        stmt = stmt.where(or_(Item.lead_type.is_(None), Item.lead_type != "seller"))
    elif lead_type:
        stmt = stmt.where(Item.lead_type == lead_type)
    if days:
        stmt = stmt.where(Item.created_at >= (datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
    if follow_up == "due":
        stmt = stmt.where(Item.follow_up_at != "", Item.follow_up_at.is_not(None), Item.follow_up_at <= NOW(),
                          Item.status.not_in(("won", "ignored")))
    elif follow_up == "scheduled":
        stmt = stmt.where(Item.follow_up_at != "", Item.follow_up_at.is_not(None))
    if tag:
        stmt = stmt.where(Item.tags.like('%' + json.dumps(tag[:30], ensure_ascii=False) + '%'))
    order = {"newest": (Item.id.desc(),), "published": (Item.published_at.desc(), Item.id.desc()),
             "follow_up": (Item.follow_up_at, Item.id.desc())}.get(sort, (Item.score.desc(), Item.id.desc()))
    return stmt.order_by(*order)


@app.get("/api/items")
def items(q: str = "", source: str = "", status: str = "", intent: str = "", author: str = "",
          sentiment: str = "", platform: str = "", kind: str = "", lead_type: str = "",
          days: int = Query(0, ge=0, le=3650), follow_up: str = "", tag: str = "",
          sort: Literal["score", "newest", "published", "follow_up"] = "score",
          limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    stmt = item_query(q, source, status, intent, author, sentiment, platform, kind, lead_type, days, follow_up, tag, sort)
    with Session() as s:
        rows = s.scalars(stmt.offset(offset).limit(limit)).all()
        return [item_dict(x) for x in rows]


@app.get("/api/items/count")
def items_count(q: str = "", source: str = "", status: str = "", intent: str = "", author: str = "",
                sentiment: str = "", platform: str = "", kind: str = "", lead_type: str = "",
                days: int = Query(0, ge=0, le=3650), follow_up: str = "", tag: str = ""):
    stmt = item_query(q, source, status, intent, author, sentiment, platform, kind, lead_type, days, follow_up, tag)
    with Session() as s:
        return {"count": s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))}


@app.get("/api/items/{item_id}")
def get_item(item_id: int):
    with Session() as s:
        item = s.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "线索不存在")
        return item_dict(item)


@app.patch("/api/items/{item_id}")
def update_item(item_id: int, patch: StatusIn):
    if patch.status is not None and patch.status not in STATUSES:
        raise HTTPException(422, "跟进状态无效")
    with Session.begin() as s:
        item = s.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "线索不存在")
        _patch_item(item, status=patch.status, notes=patch.notes, follow_up_at=patch.follow_up_at, tags=patch.tags)
        s.flush()
        return item_dict(item)


def _clean_time(value):
    """'' clears; otherwise any ISO date/time ('2026-10-30' or '2026-10-30T09:00') is stored as UTC ISO."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(422, "跟进时间格式无效") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone(timedelta(hours=8)))  # the console is used in China time
    return when.astimezone(timezone.utc).isoformat()


def _patch_item(item, status=None, notes=None, follow_up_at=None, tags=None, add_tag=None):
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(422, "跟进状态无效")
        if status == "contacted" and item.status != "contacted" and not item.contacted_at:
            item.contacted_at = NOW()
        if status in ("won", "ignored"):
            item.follow_up_at = ""
        item.status = status
    if notes is not None:
        item.notes = notes
    if follow_up_at is not None:
        item.follow_up_at = _clean_time(follow_up_at)
    if tags is not None or add_tag:
        current = tags if tags is not None else json_list(item.tags)
        if add_tag:
            current = list(current) + [add_tag]
        cleaned = list(dict.fromkeys(t.strip()[:30] for t in current if isinstance(t, str) and t.strip()))[:20]
        item.tags = json.dumps(cleaned, ensure_ascii=False)
    item.updated_at = NOW()


@app.post("/api/items/bulk")
def bulk_items(inp: BulkIn):
    """Same change for many leads at once: status, follow-up time, a tag, or delete."""
    with Session.begin() as s:
        rows = s.scalars(select(Item).where(Item.id.in_(inp.ids))).all()
        for item in rows:
            if inp.delete:
                s.delete(item)
            else:
                _patch_item(item, status=inp.status, follow_up_at=inp.follow_up_at, add_tag=inp.add_tag)
        return {"updated": len(rows), "deleted": inp.delete}


@app.get("/api/settings")
def read_settings():
    return get_settings()


@app.patch("/api/settings")
def patch_settings(body: dict):
    """Change single settings (e.g. {"browser_background": false}) without resending the word lists."""
    current = get_settings()
    unknown = set(body) - set(current)
    if unknown:
        raise HTTPException(422, "未知设置：" + "、".join(sorted(unknown)))
    return write_settings(SettingsIn(**{**current, **body}))


@app.put("/api/settings")
def write_settings(inp: SettingsIn):
    data = inp.model_dump()
    for key in ("buyer_words", "seller_words", "exclude_words", "blocked_authors"):
        data[key] = list(dict.fromkeys(w.strip()[:100] for w in data[key] if w and w.strip()))
    with Session.begin() as s:
        old = get_settings(s)
        for key, value in data.items():
            row = s.get(Setting, key)
            if row is None:
                s.add(Setting(key=key, value=json.dumps(value, ensure_ascii=False)))
            else:
                row.value = json.dumps(value, ensure_ascii=False)
    rescored = 0
    if data["buyer_words"] != old["buyer_words"] or data["seller_words"] != old["seller_words"]:
        rescored = rescore_all()  # custom intent words apply to leads already saved, too
    return {**get_settings(), "rescored": rescored}


@app.post("/api/settings/block-author")
def block_author(item_id: int):
    """Hide a competitor / spam account: future content is skipped and its saved leads are ignored."""
    with Session.begin() as s:
        item = s.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "线索不存在")
        name = (item.author_url or item.author or "").strip()
        if not name:
            raise HTTPException(422, "这条线索没有作者信息")
        settings = get_settings(s)
        blocked = list(dict.fromkeys(settings["blocked_authors"] + [name]))
        row = s.get(Setting, "blocked_authors")
        if row is None:
            s.add(Setting(key="blocked_authors", value=json.dumps(blocked, ensure_ascii=False)))
        else:
            row.value = json.dumps(blocked, ensure_ascii=False)
        cond = Item.author_url == item.author_url if item.author_url else Item.author == item.author
        hidden = 0
        for other in s.scalars(select(Item).where(cond, Item.status == "new")).all():
            other.status, other.updated_at = "ignored", NOW()
            hidden += 1
        return {"blocked": name, "ignored": hidden}


def template_dict(t):
    return {"id": t.id, "name": t.name, "text": t.text, "created_at": t.created_at}


DEFAULT_TEMPLATES = [
    ("评论区回复", "你好 {author}，看到你在问「{keyword}」，我们正好在做这个，可以免费给你出个方案，方便的话私信我～"),
    ("私信开场", "您好，我是 XX 的顾问。看到您在{platform}提到「{title}」，想了解下您现在的具体需求和预算，我可以发几个案例给您参考。"),
    ("英文回复", "Hi {author}, saw your post about {keyword}. We help teams with exactly this — happy to share a quick demo if useful."),
]


@app.get("/api/templates")
def list_templates():
    with Session.begin() as s:
        if not s.scalar(select(func.count()).select_from(ReplyTemplate)) and s.get(SchemaMeta, "templates_seeded") is None:
            for name, text in DEFAULT_TEMPLATES:
                s.add(ReplyTemplate(name=name, text=text, created_at=NOW()))
            s.add(SchemaMeta(key="templates_seeded", value="4.0"))
            s.flush()
        return [template_dict(t) for t in s.scalars(select(ReplyTemplate).order_by(ReplyTemplate.id)).all()]


@app.post("/api/templates", status_code=201)
def add_template(inp: TemplateIn):
    with Session.begin() as s:
        if s.scalar(select(func.count()).select_from(ReplyTemplate)) >= 50:
            raise HTTPException(400, "最多保存 50 条话术")
        t = ReplyTemplate(name=inp.name.strip(), text=inp.text.strip(), created_at=NOW())
        s.add(t)
        s.flush()
        return template_dict(t)


@app.put("/api/templates/{tid}")
def edit_template(tid: int, inp: TemplateIn):
    with Session.begin() as s:
        t = s.get(ReplyTemplate, tid)
        if t is None:
            raise HTTPException(404, "话术不存在")
        t.name, t.text = inp.name.strip(), inp.text.strip()
        return template_dict(t)


@app.delete("/api/templates/{tid}")
def delete_template(tid: int):
    with Session.begin() as s:
        t = s.get(ReplyTemplate, tid)
        if t is None:
            raise HTTPException(404, "话术不存在")
        s.delete(t)
    return {"ok": True}


@app.get("/api/overview")
def overview(days: int = Query(14, ge=1, le=90)):
    """Numbers for the dashboard: today, follow-ups due, funnel, per-day and per-platform counts."""
    now = datetime.now(timezone.utc)
    cn = timezone(timedelta(hours=8))
    today = (now - timedelta(days=1)).isoformat()  # "last 24 hours", the same window the lead filter uses
    since = (now - timedelta(days=days)).isoformat()
    with Session() as s:
        count = lambda *conds: s.scalar(select(func.count()).select_from(Item).where(Item.source != "Demo", *conds)) or 0
        out = {
            "today_new": count(Item.created_at >= today),
            "today_high": count(Item.created_at >= today, Item.intent == "high"),
            "high_open": count(Item.intent == "high", Item.status.in_(("new", "saved"))),
            "follow_up_due": count(Item.follow_up_at != "", Item.follow_up_at.is_not(None), Item.follow_up_at <= NOW(),
                                   Item.status.not_in(("won", "ignored"))),
            "sellers": count(Item.lead_type == "seller"),
            "total": count(),
            "funnel": {st: count(Item.status == st) for st in ("new", "saved", "contacted", "replied", "won", "ignored")},
            "monitors_active": s.scalar(select(func.count()).select_from(Monitor).where(Monitor.active.is_(True))) or 0,
        }
        daily, platforms = collections.Counter(), collections.Counter()
        daily_high = collections.Counter()
        for created, source, intent in s.execute(select(Item.created_at, Item.source, Item.intent).where(
                Item.created_at >= since, Item.source != "Demo")).all():
            try:
                day = datetime.fromisoformat(created).astimezone(cn).strftime("%m-%d")
            except (TypeError, ValueError):
                continue
            daily[day] += 1
            daily_high[day] += intent == "high"
            platforms[_platform_of(source or "")] += 1
    days_list = [(now.astimezone(cn) - timedelta(days=i)).strftime("%m-%d") for i in range(days - 1, -1, -1)]
    out["daily"] = [{"day": d, "count": daily[d], "high": daily_high[d]} for d in days_list]
    out["platforms"] = [{"platform": k, "count": v} for k, v in platforms.most_common(12)]
    return out


@app.post("/api/platforms/manual-save", status_code=201)
def save_platform_post(inp: PlatformManualIn):
    """Explicit human-assisted import; no cookies, scraping or contact-data extraction."""
    link = validate_platform_url(inp.platform, inp.url)
    source = BROWSER_PLATFORMS[inp.platform]
    content = inp.text.strip()
    if not content:
        raise HTTPException(422, "请填写原文摘要或需求描述")
    title = inp.title.strip() or content[:90]
    item = PublicItem(source, title, content, link, inp.author.strip())
    matched, inserted, ids = save_items([item], manual_query=title[:60], return_ids=True)
    with Session() as session:
        saved = item_dict(session.get(Item, ids[0])) if ids else None
    return {"source": source, "inserted": inserted, "duplicate": not bool(inserted), "item": saved, "method": "manual"}


@app.post("/api/ingest")
def ingest(inp: IngestIn):
    return do_ingest(inp)


@app.post("/api/ingest-all")
def ingest_all(query: str = ""):
    return do_ingest_all(query)




# A local opt-in browser action; no claim that a third-party URL can deep-link.
COMMENT_LOCATE_JOBS = {}
COMMENT_LOCATE_LOCK = threading.Lock()


def _comment_locate_runner(job_id, platform, post_url, comment_text, author, comment_id=""):
    def progress(status, message):
        with COMMENT_LOCATE_LOCK:
            COMMENT_LOCATE_JOBS[job_id].update(status=status, message=message)
    try:
        if not BROWSER_EXEC_LOCK.acquire(blocking=False):
            progress("busy", "浏览器采集或监控正在运行，请稍后重试定位")
            return
        try:
            locate_visible_comment(platform, post_url, comment_text, author,
                                   DATA_DIR / "browser_profile", progress, comment_id=comment_id)
        finally:
            BROWSER_EXEC_LOCK.release()
    except Exception as exc:
        progress("error", (str(exc) or type(exc).__name__)[:300])
    finally:
        with COMMENT_LOCATE_LOCK:
            job = COMMENT_LOCATE_JOBS[job_id]
            if job["status"] == "running":
                job.update(status="not_found", message="浏览器已结束，未能确定评论位置")


@app.post("/api/items/{item_id}/locate-comment", status_code=202)
def start_comment_locator(item_id: int, request: Request):
    _require_local_browser(request)
    with Session() as s:
        item = s.get(Item, item_id)
        if item is None:
            raise HTTPException(404, "线索不存在")
        if item.navigation_kind not in ("assisted", "direct") or not (item.source or "").endswith("评论"):
            raise HTTPException(422, "该线索不支持浏览器辅助定位")
        platform = next((p for p, label in BROWSER_PLATFORMS.items() if item.source == label + "评论"), None)
        post_url = item.post_url or item.url.split("#lr-comment-", 1)[0]
        if not platform or not safe_post_url(platform, post_url):
            raise HTTPException(422, "没有有效的作品地址，无法定位")
        text, author, comment_id = item.text or "", item.author or "", item.comment_id or ""
    with COMMENT_LOCATE_LOCK:
        if any(x["status"] == "running" for x in COMMENT_LOCATE_JOBS.values()):
            raise HTTPException(409, "已有一个评论定位窗口正在运行")
        jid = uuid.uuid4().hex
        COMMENT_LOCATE_JOBS[jid] = {"id": jid, "status": "running", "message": "正在打开作品并查找可见评论…"}
        for old in list(COMMENT_LOCATE_JOBS)[:-10]:
            if COMMENT_LOCATE_JOBS[old]["status"] != "running":
                COMMENT_LOCATE_JOBS.pop(old, None)
    threading.Thread(target=_comment_locate_runner, args=(jid, platform, post_url, text, author, comment_id), daemon=True).start()
    return {"id": jid, "status": "running"}


@app.get("/api/comment-locate/{job_id}")
def comment_locator_status(job_id: str, request: Request):
    _require_local_browser(request)
    with COMMENT_LOCATE_LOCK:
        if job_id not in COMMENT_LOCATE_JOBS:
            raise HTTPException(404, "定位任务不存在")
        return dict(COMMENT_LOCATE_JOBS[job_id])


def _browser_job_runner(job_id: str, query: str, platforms: list[str], max_posts: int, max_comments: int,
                        sort: str = "latest", excludes=()):
    def event(platform: str, state: str, batch: list[PublicItem], message: str):
        inserted = matched = 0
        error = ""
        if batch:
            try:
                matched, inserted = save_items(batch, manual_query=query, excludes=excludes)
            except Exception as exc:
                error = "数据库保存失败：" + str(exc)[:200]
        with BROWSER_JOB_LOCK:
            job = BROWSER_JOBS[job_id]
            report = job["platforms"].setdefault(platform, {"status": "queued", "fetched": 0,
                   "inserted": 0, "duplicates": 0, "message": "", "error": ""})
            report["status"] = state
            report["message"] = message
            report["fetched"] += len(batch)
            report["inserted"] += inserted
            report["duplicates"] += max(0, matched - inserted)
            if error or state == "error":
                report["error"] = error or message
            job["events"] = (job["events"] + [f"{_source_name(platform)}：{message}"])[-24:]
        if state in ("done", "error", "empty"):
            log_ingest(("browser-" + platform) if platform in BROWSER_PLATFORMS else platform, query, fetched=report["fetched"],
                       matched=report["inserted"] + report["duplicates"],
                       inserted=report["inserted"], error=report["error"] if state != "empty" else message)

    browser_selected = [p for p in platforms if p in BROWSER_PLATFORMS]
    api_selected = [p for p in platforms if p not in BROWSER_PLATFORMS]
    try:
        # Public/authorized APIs first: quick results while the browser part runs.
        for platform in api_selected:
            event(platform, "searching", [], "正在请求接口…")
            try:
                data = monitor_fetch(platform, query, max_posts, max_comments)
                event(platform, "reading", data, f"接口返回 {len(data)} 条")
                event(platform, "done", [], f"完成：获取 {len(data)} 条")
            except Exception as exc:
                event(platform, "error", [], (str(exc) or type(exc).__name__)[:260])
        if browser_selected:
            if not BROWSER_EXEC_LOCK.acquire(blocking=False):
                with BROWSER_JOB_LOCK:
                    BROWSER_JOBS[job_id]["events"].append("监控任务正在使用浏览器，本次采集会在它结束后自动开始…")
                BROWSER_EXEC_LOCK.acquire()
            try:
                collect_in_browser(query, browser_selected, DATA_DIR / "browser_profile", event,
                                   max_posts=max_posts, comments_per_post=max_comments,
                                   login_wait_seconds=180, sort=sort,
                                   background=get_settings()["browser_background"])
            finally:
                BROWSER_EXEC_LOCK.release()
    except Exception as exc:
        with BROWSER_JOB_LOCK:
            job = BROWSER_JOBS[job_id]
            job["error"] = str(exc)[:380]
    finally:
        with BROWSER_JOB_LOCK:
            BROWSER_JOBS[job_id]["status"] = "finished"


def _source_name(source_id: str) -> str:
    if source_id in BROWSER_PLATFORMS:
        return BROWSER_PLATFORMS[source_id]
    return next((x["name"] for x in monitor_source_catalog() if x["id"] == source_id), source_id)


def _start_search_job(query: str, platforms: list[str], max_posts: int, max_comments: int, sort: str, excludes=()):
    with BROWSER_JOB_LOCK:
        if any(job["status"] == "running" for job in BROWSER_JOBS.values()):
            raise HTTPException(409, "已有一个搜索任务正在进行，请等它完成")
        jid = uuid.uuid4().hex
        BROWSER_JOBS[jid] = {"id": jid, "query": query, "status": "running", "error": "",
                             "platforms": {p: {"status": "queued", "fetched": 0, "inserted": 0,
                                                "duplicates": 0, "message": "排队等待", "error": "",
                                                "name": _source_name(p)}
                                           for p in platforms},
                             "events": ["搜索任务已建立。"], "created_at": NOW()}
        # Old completed task status is not needed across app sessions.
        for old in list(BROWSER_JOBS)[:-12]:
            if old != jid and BROWSER_JOBS[old]["status"] == "finished":
                del BROWSER_JOBS[old]
    threading.Thread(target=_browser_job_runner,
                     args=(jid, query, platforms, max_posts, max_comments, sort, excludes), daemon=True).start()
    return {"id": jid, "query": query, "status": "running"}


class SearchNowIn(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    platforms: list[str] = Field(min_length=1, max_length=40)
    max_posts: int = Field(default=10, ge=1, le=20)
    max_comments: int = Field(default=20, ge=0, le=30)
    sort: Literal["latest", "relevance"] = "latest"
    excludes: list[str] = Field(default_factory=list, max_length=30)


@app.post("/api/search-now", status_code=202)
def search_now(inp: SearchNowIn, request: Request):
    """One-off search over any mix of browser platforms and configured API sources."""
    query = inp.query.strip()
    platforms = list(dict.fromkeys(inp.platforms))
    catalog = {r["id"]: r for r in monitor_source_catalog()}
    if not query or any(p not in catalog for p in platforms):
        raise HTTPException(422, "请填写关键词并选择有效来源")
    missing = [catalog[p]["name"] for p in platforms if not catalog[p]["configured"]]
    if missing:
        raise HTTPException(422, "以下来源尚未配置或不可用：" + "、".join(missing))
    if any(p in BROWSER_PLATFORMS for p in platforms):
        _require_local_browser(request)
    return _start_search_job(query, platforms, inp.max_posts, inp.max_comments, inp.sort, clean_words(inp.excludes))


class BrowserLoginIn(BaseModel):
    platforms: list[str] = Field(min_length=1, max_length=8)


LOGIN_JOB = {"status": "idle", "message": "", "states": {}}


@app.post("/api/browser-login", status_code=202)
def browser_login(inp: BrowserLoginIn, request: Request):
    """Open the collector's own browser profile on each platform so the user signs in by hand."""
    _require_local_browser(request)
    platforms = [p for p in dict.fromkeys(inp.platforms) if p in BROWSER_PLATFORMS]
    if not platforms:
        raise HTTPException(422, "请选择要登录的社交平台")
    if not browser_available():
        raise HTTPException(422, "未安装浏览器组件，请关闭程序后重新双击 start.bat 自动安装")
    if not BROWSER_EXEC_LOCK.acquire(blocking=False):
        raise HTTPException(409, "浏览器正被搜索/监控/定位任务使用，请稍后再试")

    def progress(status, message):
        LOGIN_JOB.update(status=status, message=message)

    def runner():
        try:
            states = open_login_pages(platforms, DATA_DIR / "browser_profile", progress)
            if isinstance(states, dict):
                LOGIN_JOB["states"] = states
        except Exception as exc:
            progress("error", (str(exc) or type(exc).__name__)[:300])
        finally:
            BROWSER_EXEC_LOCK.release()
            if LOGIN_JOB["status"] == "running":
                progress("done", "登录窗口已关闭，登录状态已保存")

    LOGIN_JOB["states"] = {}
    progress("running", "正在打开浏览器…")
    threading.Thread(target=runner, daemon=True).start()
    return dict(LOGIN_JOB)


@app.get("/api/browser-login-state")
def browser_login_state():
    """Which platforms the collector browser is signed in to (from the saved cookies on disk)."""
    profile = DATA_DIR / "browser_profile"
    busy = BROWSER_EXEC_LOCK.locked()
    states = {} if busy else saved_login_states(profile)
    channel = browser_channel(profile) if browser_available() or not busy else None
    return {"states": states, "busy": busy, "browser": BROWSER_NAMES.get(channel, "浏览器"),
            "background": get_settings()["browser_background"]}


@app.post("/api/network-check")
def network_check(request: Request):
    """Why can't the collector browser open douyin.com? DNS / direct / proxy / browser checks."""
    _require_local_browser(request)
    from .netcheck import run_check
    result = run_check(include_browser=browser_available())
    result.update(version="4.1.1", build=BUILD_ID, root=str(ROOT))
    return result


@app.post("/api/network-fix")
def network_fix(request: Request):
    """Clear the Windows DNS cache (left-over fake addresses after a VPN's TUN mode)."""
    _require_local_browser(request)
    from .netcheck import flush_dns
    ok, output = flush_dns()
    return {"ok": ok, "output": output}


@app.get("/api/browser-login")
def browser_login_status():
    return dict(LOGIN_JOB)


def monitor_dict(m: Monitor):
    return {"id": m.id, "query": m.query, "platforms": json_list(m.platforms),
            "interval_minutes": m.interval_minutes, "max_posts": m.max_posts,
            "max_comments": m.max_comments, "sort": m.sort or "latest", "active": bool(m.active),
            "excludes": json_list(m.excludes),
            "created_at": m.created_at, "last_run_at": m.last_run_at,
            "last_success_at": m.last_success_at, "next_run_at": m.next_run_at,
            "last_status": m.last_status, "last_error": m.last_error,
            "last_fetched": m.last_fetched, "last_new": m.last_new,
            "run_count": m.run_count, "last_run_id": m.last_run_id}


def _require_local_browser(request: Request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "本机浏览器监控只允许在运行程序的电脑本地设置")


MONITOR_HEADLESS = os.getenv("MONITOR_HEADLESS", "0") == "1"


def _monitor_once(monitor_id: int):
    """One bounded poll. Browser and sign-in profile are exclusively owned during run.

    Monitors that only use APIs never wait for the browser. Returns False when the
    browser is busy (the monitor stays due and is retried shortly).
    The run is a discovery delta, NOT proof of the third party's publish time.
    """
    with Session() as session:
        m = session.get(Monitor, monitor_id)
        if not m or not m.active:
            return False
        platforms = json_list(m.platforms)
    browser_selected = [p for p in platforms if p in BROWSER_PLATFORMS]
    holds_browser = False
    if browser_selected:
        if not BROWSER_EXEC_LOCK.acquire(blocking=False):
            return False  # don't contend with a human browser collection task
        holds_browser = True
    try:
        with Session.begin() as session:
            m = session.get(Monitor, monitor_id)
            if not m or not m.active:
                return False
            query, platforms = m.query, json_list(m.platforms)
            max_posts, max_comments, sort = m.max_posts, m.max_comments, (m.sort or "latest")
            excludes = json_list(m.excludes)
            m.last_status = "running"
            # Make this run non-overlapping even if scheduler wakes again.
            m.next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=m.interval_minutes)).isoformat()
        try:
            _run_monitor(monitor_id, query, platforms, max_posts, max_comments, sort, excludes)
        except Exception as exc:
            # Never leave a task stuck in "running" (which blocks "立即检查").
            with Session.begin() as session:
                m = session.get(Monitor, monitor_id)
                if m:
                    m.last_status = "error"
                    m.last_error = ("监控执行异常：" + (str(exc) or type(exc).__name__))[:900]
                    m.last_run_at = NOW()
            raise
        return True
    finally:
        if holds_browser:
            BROWSER_EXEC_LOCK.release()


def _run_monitor(monitor_id, query, platforms, max_posts, max_comments, sort, excludes=()):
    reports = {p: {"status": "queued", "fetched": 0, "inserted": 0, "error": ""} for p in platforms}
    fatal = ""

    def event(platform, state, batch, message):
        r = reports[platform]
        r["status"] = state
        r["message"] = message[:260]
        if batch:
            try:
                matched, inserted = save_items(batch, manual_query=query, excludes=excludes)
                r["fetched"] += len(batch)
                r["inserted"] += inserted
            except Exception as exc:
                r["error"] = "数据库写入失败：" + str(exc)[:180]
        if state in ("error", "empty"):
            r["error"] = message[:240]

    browser_selected = [p for p in platforms if p in BROWSER_PLATFORMS]
    api_selected = [p for p in platforms if p not in BROWSER_PLATFORMS]
    if browser_selected:
        try:
            collect_in_browser(query, browser_selected, DATA_DIR / "browser_profile", event,
                               max_posts=max_posts, comments_per_post=max_comments,
                               headless=MONITOR_HEADLESS,
                               login_wait_seconds=int(os.getenv("MONITOR_LOGIN_WAIT_SECONDS", "60")),
                               sort=sort, background=get_settings()["browser_background"])
        except Exception as exc:
            # Do not hide the failure behind a successful public-source poll.
            for p in browser_selected:
                if reports[p]["status"] not in ("done", "empty", "error"):
                    event(p, "error", [], str(exc)[:250])
            fatal = str(exc)[:300]
    configured_sources = {row["id"]: row for row in monitor_source_catalog()}
    for platform in api_selected:
        try:
            if not configured_sources.get(platform, {}).get("configured"):
                raise RuntimeError("未配置授权接口或 API 密钥")
            data = monitor_fetch(platform, query, max_posts, max_comments)
            event(platform, "reading", data, f"接口返回 {len(data)} 条")
            if reports[platform]["error"]:
                event(platform, "error", [], reports[platform]["error"])
            else:
                event(platform, "done", [], f"本轮完成：获取 {len(data)} 条，新增 {reports[platform]['inserted']} 条")
        except Exception as exc:
            event(platform, "error", [], str(exc)[:260] or type(exc).__name__)
    fetched = sum(r["fetched"] for r in reports.values())
    new_count = sum(r["inserted"] for r in reports.values())
    successes = sum(1 for r in reports.values() if r["status"] == "done" and not r["error"])
    display_names = {x["id"]: x["name"] for x in monitor_source_catalog()}
    errors = [f"{display_names.get(p,p)}：{r['error']}" for p,r in reports.items() if r["error"]]
    if fatal:
        errors.append(fatal)
    status = "ok" if successes == len(platforms) else "partial" if successes else "error"
    finished = NOW()
    with Session.begin() as session:
        m = session.get(Monitor, monitor_id)
        if not m:
            return
        run = MonitorRun(monitor_id=monitor_id, finished_at=finished, fetched=fetched,
                         inserted=new_count, status=status, details=json.dumps(reports, ensure_ascii=False))
        session.add(run)
        session.flush()
        m.last_run_id = run.id
        m.run_count = (m.run_count or 0) + 1
        m.last_run_at = finished
        m.last_status = status if m.active else "paused"
        m.last_error = "；".join(errors)[:900]
        m.last_fetched = fetched
        m.last_new = new_count
        if successes:
            m.last_success_at = finished
        m.next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=m.interval_minutes)).isoformat() if m.active else ""
        # Retain the latest 250 run reports across all monitors.
        old_ids = session.scalars(select(MonitorRun.id).order_by(MonitorRun.id.desc()).offset(250)).all()
        if old_ids:
            session.execute(delete(MonitorRun).where(MonitorRun.id.in_(old_ids)))
    log_ingest("monitor", query, fetched=fetched, matched=fetched, inserted=new_count,
               error="；".join(errors)[:900])


async def monitor_scheduler():
    """Process persisted due monitors sequentially; stop when the server stops.
    A monitor waiting for the busy browser doesn't block API-only monitors behind it."""
    while True:
        executed = False
        try:
            now = NOW()
            with Session() as session:
                due = session.scalars(select(Monitor.id).where(Monitor.active.is_(True), Monitor.next_run_at != "",
                         Monitor.next_run_at <= now).order_by(Monitor.next_run_at, Monitor.id).limit(20)).all()
            for monitor_id in due:
                try:
                    if await asyncio.to_thread(_monitor_once, monitor_id):
                        executed = True
                        break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    executed = True  # error recorded on the monitor; move on
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            # Don't crash FastAPI on a transient database or browser exception.
            pass
        await asyncio.sleep(1 if executed else 3)


@app.get("/api/monitors")
def list_monitors():
    with Session() as s:
        return [monitor_dict(m) for m in s.scalars(select(Monitor).order_by(Monitor.id.desc())).all()]


@app.post("/api/monitors", status_code=201)
def add_monitor(inp: MonitorIn, request: Request):
    _require_local_browser(request)
    query = inp.query.strip()
    selected = list(dict.fromkeys(inp.platforms))
    catalog = {r["id"]: r for r in monitor_source_catalog()}
    if not query or not selected or any(p not in catalog for p in selected):
        raise HTTPException(422, "必须填写关键词并选择有效监控来源")
    missing = [catalog[p]["name"] for p in selected if not catalog[p]["configured"]]
    if missing:
        raise HTTPException(422, "以下平台尚未配置授权接口或密钥：" + "、".join(missing))
    with Session.begin() as s:
        if s.scalar(select(func.count()).select_from(Monitor)) >= 15:
            raise HTTPException(400, "最多创建15个监控任务，避免频繁请求平台")
        m = Monitor(query=query, platforms=json.dumps(selected), interval_minutes=inp.interval_minutes,
                    max_posts=inp.max_posts, max_comments=inp.max_comments, sort=inp.sort, active=True,
                    excludes=json.dumps(clean_words(inp.excludes), ensure_ascii=False),
                    created_at=NOW(), next_run_at=NOW(), last_status="pending")
        s.add(m)
        s.flush()
        out = monitor_dict(m)
    return out


@app.patch("/api/monitors/{monitor_id}")
def patch_monitor(monitor_id: int, inp: MonitorPatch, request: Request):
    _require_local_browser(request)
    with Session.begin() as s:
        m = s.get(Monitor, monitor_id)
        if not m: raise HTTPException(404, "监控任务不存在")
        if inp.platforms is not None:
            selected = list(dict.fromkeys(inp.platforms))
            catalog = {r["id"]: r for r in monitor_source_catalog()}
            if any(p not in catalog for p in selected):
                raise HTTPException(422, "包含无效的监控来源")
            missing = [catalog[p]["name"] for p in selected if not catalog[p]["configured"]]
            if missing:
                raise HTTPException(422, "以下平台尚未配置授权接口或密钥：" + "、".join(missing))
            m.platforms = json.dumps(selected)
        if inp.query is not None:
            if not inp.query.strip():
                raise HTTPException(422, "关键词不能为空")
            m.query = inp.query.strip()
        for field in ("interval_minutes", "max_posts", "max_comments", "sort"):
            if getattr(inp, field) is not None:
                setattr(m, field, getattr(inp, field))
        if inp.excludes is not None:
            m.excludes = json.dumps(clean_words(inp.excludes), ensure_ascii=False)
        if inp.active is not None:
            m.active = inp.active
            m.next_run_at = NOW() if inp.active else ""
            m.last_status = "pending" if inp.active else "paused"
        elif inp.interval_minutes is not None and m.active and m.last_status != "running":
            m.next_run_at = (datetime.now(timezone.utc) + timedelta(minutes=m.interval_minutes)).isoformat()
        out = monitor_dict(m)
    return out


@app.post("/api/monitors/{monitor_id}/run")
def run_monitor_now(monitor_id: int, request: Request):
    _require_local_browser(request)
    with Session.begin() as s:
        m = s.get(Monitor, monitor_id)
        if not m: raise HTTPException(404, "监控任务不存在")
        if not m.active: raise HTTPException(409, "先启用监控任务")
        if m.last_status == "running": raise HTTPException(409, "任务正在采集")
        m.next_run_at = NOW()
    return {"queued": True}


@app.delete("/api/monitors/{monitor_id}")
def remove_monitor(monitor_id: int, request: Request):
    _require_local_browser(request)
    with Session.begin() as s:
        m = s.get(Monitor, monitor_id)
        if not m: raise HTTPException(404, "监控任务不存在")
        s.delete(m)
    return {"deleted": True}


@app.get("/api/monitor-runs")
def monitor_runs(limit: int = Query(25, ge=1, le=100)):
    with Session() as s:
        rows = s.scalars(select(MonitorRun).order_by(MonitorRun.id.desc()).limit(limit)).all()
        return [{"id": x.id, "monitor_id": x.monitor_id, "finished_at": x.finished_at,
                 "fetched": x.fetched, "inserted": x.inserted, "status": x.status,
                 "details": json.loads(x.details or '{}')} for x in rows]


@app.post("/api/browser-crawl/start", status_code=202)
def start_browser_crawl(inp: BrowserSearchIn, request: Request):
    """Local desktop-only human-supervised search; does not bypass platform controls."""
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "为保护本机浏览器登录状态，只允许在运行程序的电脑本地启动采集")
    query = inp.query.strip()
    if not query:
        raise HTTPException(422, "关键词不能为空")
    platforms = list(dict.fromkeys(inp.platforms))
    if not platforms or any(platform not in BROWSER_PLATFORMS for platform in platforms):
        raise HTTPException(422, "只支持：" + "、".join(BROWSER_PLATFORMS.values()))
    return _start_search_job(query, platforms, inp.max_posts, inp.max_comments, inp.sort)


@app.get("/api/browser-crawl-active")
def active_browser_crawl():
    """Lets a reloaded page re-attach to the search that is still running."""
    with BROWSER_JOB_LOCK:
        running = [j["id"] for j in BROWSER_JOBS.values() if j["status"] == "running"]
    return {"id": running[-1]} if running else {}


@app.get("/api/browser-crawl/{job_id}")
def get_browser_crawl(job_id: str):
    with BROWSER_JOB_LOCK:
        if job_id not in BROWSER_JOBS:
            raise HTTPException(404, "任务不存在或服务已重启")
        return json.loads(json.dumps(BROWSER_JOBS[job_id], ensure_ascii=False))


@app.post("/api/search-online")
def search_online(inp: OnlineSearchIn):
    """Real external query, not a filter on empty local storage.

    Search public providers concurrently; save sequentially (SQLite-safe).
    Every result is recorded with its source, including zero hits and failures.
    """
    query = inp.query.strip()
    if not query:
        raise HTTPException(422, "请输入要搜索的关键词")
    selected = list(dict.fromkeys(inp.sources))
    if any(source not in PUBLIC_SEARCH_SOURCES for source in selected):
        raise HTTPException(422, "在线搜索只允许选择已实现的公开数据源")
    providers = SEARCH_PROVIDERS
    reports, ids = [], []
    with ThreadPoolExecutor(max_workers=min(5, len(selected))) as executor:
        futures = {source: executor.submit(providers[source], query) for source in selected}
        for source in selected:
            try:
                raw = futures[source].result()
                data = raw[:50]
                matched, inserted, item_ids = save_items(data, manual_query=query, return_ids=True)
                ids.extend(item_ids)
                report = {"source": source, "fetched": len(data), "matched": matched,
                          "inserted": inserted, "duplicates": matched - inserted, "error": ""}
                log_ingest(source, query, fetched=len(data), matched=matched, inserted=inserted)
            except Exception as exc:
                message = str(exc)[:300] or type(exc).__name__
                report = {"source": source, "fetched": 0, "matched": 0,
                          "inserted": 0, "duplicates": 0, "error": message}
                log_ingest(source, query, error=message)
            reports.append(report)
    unique_ids = list(dict.fromkeys(ids))[:200]
    with Session() as session:
        by_id = {item.id: item_dict(item) for item in session.scalars(
            select(Item).where(Item.id.in_(unique_ids))).all()} if unique_ids else {}
    return {"query": query, "reports": reports, "items": [by_id[i] for i in unique_ids if i in by_id],
            "count": len(unique_ids), "is_live": True}


@app.get("/api/sources")
def sources():
    return available_sources()


@app.get("/api/ingest-logs")
def ingest_logs(limit: int = Query(30, ge=1, le=200)):
    with Session() as s:
        rows = s.scalars(select(IngestLog).order_by(IngestLog.id.desc()).limit(limit)).all()
        return [{"id": x.id, "source": x.source, "query": x.query, "fetched": x.fetched,
                 "matched": x.matched, "inserted": x.inserted, "error": x.error, "finished_at": x.finished_at} for x in rows]


@app.get("/api/stats")
def stats():
    with Session() as s:
        return {
            "total": s.scalar(select(func.count()).select_from(Item)),
            "high": s.scalar(select(func.count()).select_from(Item).where(Item.intent == "high")),
            "new": s.scalar(select(func.count()).select_from(Item).where(Item.status == "new")),
            "demo": s.scalar(select(func.count()).select_from(Item).where(Item.source == "Demo")),
            "real": s.scalar(select(func.count()).select_from(Item).where(Item.source != "Demo")),
        }


@app.get("/api/export.csv")
def export_csv(q: str = "", source: str = "", status: str = "", intent: str = "", author: str = "",
               sentiment: str = "", platform: str = "", kind: str = "", lead_type: str = "",
               days: int = Query(0, ge=0, le=3650), follow_up: str = "", tag: str = "",
               sort: Literal["score", "newest", "published", "follow_up"] = "score"):
    """Exports exactly what the lead list shows with the same filters (no filters = everything)."""
    cols = ("id", "source", "title", "text", "url", "post_url", "comment_url", "author", "author_url", "published_at",
            "like", "collect", "comment", "share", "view", "matched_terms", "intent", "score", "lead_type", "reasons",
            "status", "follow_up_at", "contacted_at", "tags", "created_at", "sentiment", "topics", "hashtags", "notes")
    stmt = item_query(q, source, status, intent, author, sentiment, platform, kind, lead_type, days, follow_up, tag, sort)
    with Session() as s:
        rows = s.scalars(stmt).all()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for item in rows:
            record = []
            counters = _json_dict(item.metrics)
            for col in cols:
                val = counters.get(col, "") if col in ("like", "collect", "comment", "share", "view") else (getattr(item, col) or "")
                if isinstance(val, str) and val.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    val = "'" + val  # prevent formulas executing when opened in Excel
                record.append(val)
            writer.writerow(record)
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=lead-radar.csv"})


def _platform_of(source: str) -> str:
    for suffix in ("作品", "评论", "视频"):
        if source.endswith(suffix):
            return source[:-len(suffix)]
    return source


@app.get("/api/authors")
def authors(q: str = "", platform: str = "", min_items: int = Query(1, ge=1, le=100),
            limit: int = Query(100, ge=1, le=500)):
    """Public posters/commenters whose saved content matched a keyword.

    This is NOT "people who searched the keyword" (platforms never expose that);
    it aggregates authors of public works/comments already collected.
    """
    stmt = select(Item).where(Item.author.is_not(None), Item.author != "", Item.source != "Demo",
                              Item.source.not_in(("Google News", "Bing News")))
    if q:
        pattern = f"%{q[:200]}%"
        stmt = stmt.where(or_(Item.matched_terms.ilike(pattern), Item.title.ilike(pattern), Item.text.ilike(pattern)))
    groups = {}
    with Session() as s:
        for item in s.scalars(stmt.order_by(Item.id.desc()).limit(20000)):
            plat = _platform_of(item.source or "")
            if platform and plat != platform:
                continue
            key = (plat, item.author_url or ("name:" + (item.author or "").strip()))
            g = groups.setdefault(key, {"platform": plat, "author": item.author, "author_url": item.author_url or "",
                                        "works": 0, "comments": 0, "high_intent": 0, "max_score": 0,
                                        "likes_received": 0, "last_seen": "", "latest_text": "", "latest_url": "",
                                        "item_ids": [], "statuses": collections.Counter()})
            is_comment = (item.source or "").endswith("评论")
            g["comments" if is_comment else "works"] += 1
            g["high_intent"] += item.intent == "high"
            g["max_score"] = max(g["max_score"], item.score or 0)
            g["likes_received"] += int(_json_dict(item.metrics).get("like") or 0)
            g["statuses"][item.status or "new"] += 1
            if len(g["item_ids"]) < 10:
                g["item_ids"].append(item.id)
            if not g["last_seen"] or (item.created_at or "") > g["last_seen"]:
                g["last_seen"] = item.created_at or ""
                g["latest_text"] = (item.text or item.title or "")[:200]
                g["latest_url"] = item.comment_url or item.post_url or item.url or ""
            if item.author_url and not g["author_url"]:
                g["author_url"] = item.author_url
    rows = [dict(g, total=g["works"] + g["comments"], statuses=dict(g["statuses"]))
            for g in groups.values() if g["works"] + g["comments"] >= min_items]
    rows.sort(key=lambda g: (g["high_intent"], g["max_score"], g["total"], g["last_seen"]), reverse=True)
    return rows[:limit]


@app.get("/api/authors.csv")
def authors_csv(q: str = "", platform: str = ""):
    rows = authors(q=q, platform=platform, min_items=1, limit=500)
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ("platform", "author", "author_url", "works", "comments", "high_intent", "max_score",
            "likes_received", "last_seen", "latest_text", "latest_url")
    writer.writerow(cols)
    for row in rows:
        values = []
        for col in cols:
            val = row.get(col, "")
            if isinstance(val, str) and val.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                val = "'" + val
            values.append(val)
        writer.writerow(values)
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=lead-radar-authors.csv"})


@app.get("/api/platforms")
def platforms_in_db():
    with Session() as s:
        rows = s.execute(select(Item.source, func.count()).group_by(Item.source)).all()
    counts = collections.Counter()
    for source, n in rows:
        counts[_platform_of(source or "")] += n
    return [{"platform": k, "count": v} for k, v in counts.most_common()]


@app.get("/api/trends")
def trends():
    with Session() as s:
        rows = s.scalars(select(Item)).all()
        sources = collections.Counter(x.source for x in rows)
        sentiments = collections.Counter(x.sentiment for x in rows)
        topics = collections.Counter()
        tags = collections.Counter()
        for x in rows:
            topics.update(json_list(x.topics))
            tags.update(json_list(x.hashtags))
        return {"sources": dict(sources), "sentiments": dict(sentiments),
                "topics": topics.most_common(20), "hashtags": tags.most_common(20)}


@app.post("/api/alerts/test")
def test_alert():
    url = os.getenv("ALERT_WEBHOOK_URL", "")
    if not url:
        return {"sent": False, "reason": "未配置 ALERT_WEBHOOK_URL"}
    if urlparse(url).scheme != "https":
        return {"sent": False, "reason": "Webhook 仅支持 HTTPS"}
    try:
        r = requests.post(url, json=alert_payload(url, [{"source": "测试", "title": "这是一条测试通知", "text": "",
                                                         "author": "Lead Radar", "url": "https://example.com/",
                                                         "score": 100, "reasons": ["测试"]}]), timeout=10)
        return {"sent": r.ok, "status_code": r.status_code, "response": r.text[:200]}
    except requests.RequestException as exc:
        return {"sent": False, "reason": str(exc)[:200]}
