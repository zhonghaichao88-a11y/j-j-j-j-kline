"""Public and authorized content adapters. No login/CAPTCHA bypassing."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode, urljoin
import hashlib
import html
import os
import re
import requests
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class PublicItem:
    source: str
    title: str
    text: str
    url: str
    author: str = ""
    published_at: str = ""
    post_url: str = ""
    comment_id: str = ""
    comment_url: str = ""
    navigation_kind: str = ""  # direct or assisted; empty for ordinary posts
    dedup_token: str = ""     # preserve v3 browser-comment unique keys
    author_url: str = ""      # public profile page of the poster/commenter, when shown
    # Public engagement counters shown by the platform (like/collect/comment/share...).
    metrics: dict = field(default_factory=dict, compare=False, hash=False)


def _now():
    return datetime.now(timezone.utc).isoformat()


def demo_items():
    return [
        PublicItem("Demo", "求推荐 AI 客服接入方案", "公司想接入 GPT 客服，有没有靠谱团队和预算参考？", "https://example.com/demo/1", "demo-buyer", _now()),
        PublicItem("Demo", "自动化获客工具讨论", "正在比较关键词监测和客户线索工具，想看看实际效果。", "https://example.com/demo/2", "demo-founder", _now()),
        PublicItem("Demo", "GPT 学习分享", "今天学习了提示词，没有购买需求。", "https://example.com/demo/3", "demo-learner", _now()),
    ]


def _get(url, *, params=None, headers=None, timeout=20, redirect_hosts=()):
    """GET without blind redirects. Known regional redirects (e.g. www.bing.com ->
    cn.bing.com) are followed only when the target stays on an allowed HTTPS host."""
    headers = headers or {"User-Agent": "LeadRadar/2.0 (+public-content-monitor)"}
    for _ in range(4):
        r = requests.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=False)
        if not 300 <= r.status_code < 400:
            r.raise_for_status()
            return r
        target = urljoin(r.url or url, r.headers.get("Location", ""))
        host = (urlparse(target).hostname or "").lower()
        if not (redirect_hosts and urlparse(target).scheme == "https" and
                any(host == h or host.endswith("." + h) for h in redirect_hosts)):
            raise ValueError("数据源地址发生重定向，请使用其最终 HTTPS 地址")
        url, params = target, None
    raise ValueError("数据源重定向次数过多")


def to_count(value):
    """Parse public counters such as 1.2万 / 3.4w / 10万+ / 1,234 / 2.1K into int."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("，", ",")
    m = re.search(r"(\d+(?:,\d{3})+|\d+(?:\.\d+)?)\s*(万|w|W|亿|千|k|K|m|M)?", text)
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    unit = m.group(2) or ""
    scale = {"万": 1e4, "w": 1e4, "W": 1e4, "亿": 1e8, "千": 1e3, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}.get(unit, 1)
    return int(round(number * scale))


def clean_metrics(raw):
    """Keep known counters only, as integers."""
    out = {}
    for key in ("like", "collect", "comment", "share", "coin", "view", "repost", "quote"):
        n = to_count((raw or {}).get(key))
        if n is not None and n >= 0:
            out[key] = n
    return out


def _clean_markup(s):
    # Algolia/HN and RSS return HTML; decode entities such as &#x27; after stripping tags.
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(s or "")))).strip()


def fetch_rss(url: str, source: str = "RSS", redirect_hosts=()):
    """RSS 2.0 and Atom feeds. The endpoint validates user-supplied feed URLs."""
    r = _get(url, timeout=20, redirect_hosts=redirect_hosts)
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise ValueError("RSS/Atom XML 格式无效") from e

    def node_text(node, name):
        el = node.find(name)
        return (el.text or "").strip() if el is not None and el.text else ""

    out = []
    if root.tag.endswith("rss") or root.tag.endswith("RDF"):
        entries = root.findall(".//item")
        for e in entries[:100]:
            out.append(PublicItem(source, node_text(e, "title"), _clean_markup(node_text(e, "description")), node_text(e, "link"), node_text(e, "author"), node_text(e, "pubDate")))
    elif root.tag.endswith("feed"):
        ns = "{http://www.w3.org/2005/Atom}"
        for e in root.findall(f"{ns}entry")[:100]:
            links = e.findall(f"{ns}link")
            link = next((x.attrib.get("href", "") for x in links if x.attrib.get("rel", "alternate") == "alternate"), "")
            author = e.find(f"{ns}author")
            out.append(PublicItem(source, node_text(e, f"{ns}title"), _clean_markup(node_text(e, f"{ns}summary") or node_text(e, f"{ns}content")), link,
                                  node_text(author, f"{ns}name") if author is not None else "", node_text(e, f"{ns}published") or node_text(e, f"{ns}updated")))
    else:
        raise ValueError("不支持的订阅格式，需要 RSS 2.0 或 Atom")
    return out


def fetch_reddit_json(query: str):
    r = _get("https://www.reddit.com/search.json", params={"q": query, "sort": "new", "limit": 50},
             headers={"User-Agent": "LeadRadar/2.0 public-feed-aggregator"})
    out = []
    for child in r.json().get("data", {}).get("children", []):
        d = child.get("data", {})
        path = d.get("permalink", "")
        if not str(path).startswith("/"):
            continue
        published = datetime.fromtimestamp(d["created_utc"], timezone.utc).isoformat() if d.get("created_utc") else ""
        author = str(d.get("author") or "")
        out.append(PublicItem("Reddit", d.get("title", ""), d.get("selftext", ""), "https://www.reddit.com" + path, author, published,
                              author_url=_reddit_user(author),
                              metrics=clean_metrics({"like": d.get("score"), "comment": d.get("num_comments")})))
    return out


def _reddit_user(name):
    return f"https://www.reddit.com/user/{name}" if re.fullmatch(r"[A-Za-z0-9_-]{3,20}", name or "") else ""


def fetch_youtube(query: str, include_comments: bool = False, max_videos: int = 5,
                  max_comments: int = 20):
    """Official YouTube video search and bounded, publicly visible top-level comments.

    Comments require a YouTube Data API key, may be disabled, and are never
    assumed to be a complete thread. API/permission failure propagates rather
    than silently reporting that comments have been collected.
    """
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        raise RuntimeError("未配置 YOUTUBE_API_KEY；YouTube 官方接口需要 Google API 密钥")
    r = _get("https://www.googleapis.com/youtube/v3/search", params={
        "part": "snippet", "q": query, "type": "video", "order": "date",
        "maxResults": min(50, max(1, int(max_videos))), "key": key})
    out = []
    rows = r.json().get("items", [])
    ids = [str(x.get("id", {}).get("videoId")) for x in rows
           if re.fullmatch(r"[a-zA-Z0-9_-]{11}", str(x.get("id", {}).get("videoId") or ""))]
    stats = {}
    if ids:
        try:  # 1 quota unit for up to 50 videos; counters are optional
            sr = _get("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "statistics", "id": ",".join(ids[:50]), "key": key})
            stats = {v["id"]: v.get("statistics") or {} for v in sr.json().get("items", [])
                     if isinstance(v, dict) and isinstance(v.get("id"), str)}
        except (requests.RequestException, ValueError, AttributeError):
            stats = {}
    for row in rows:
        video = row.get("id", {}).get("videoId")
        if not video or not re.fullmatch(r"[a-zA-Z0-9_-]{11}", str(video)):
            continue
        s = row.get("snippet", {})
        url = f"https://www.youtube.com/watch?v={video}"
        st = stats.get(video, {})
        channel = str(s.get("channelId") or "")
        out.append(PublicItem("YouTube视频", s.get("title", ""), s.get("description", ""),
                              url, s.get("channelTitle", ""), s.get("publishedAt", ""),
                              author_url=f"https://www.youtube.com/channel/{channel}" if re.fullmatch(r"[A-Za-z0-9_-]{10,40}", channel) else "",
                              metrics=clean_metrics({"view": st.get("viewCount"), "like": st.get("likeCount"),
                                                     "comment": st.get("commentCount"), "collect": st.get("favoriteCount")})))
        if not include_comments or not max_comments:
            continue
        try:
            cr = _get("https://www.googleapis.com/youtube/v3/commentThreads", params={
                "part": "snippet", "videoId": video, "maxResults": min(100, max_comments),
                "order": "time", "textFormat": "plainText", "key": key})
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (403, 404):
                # Comments may be disabled for individual videos. Keep video.
                continue
            raise
        for thread in cr.json().get("items", []):
            top = thread.get("snippet", {}).get("topLevelComment", {})
            cid = top.get("id")
            details = top.get("snippet", {})
            if not cid or not re.fullmatch(r"[a-zA-Z0-9_-]+", str(cid)):
                continue
            msg = (details.get("textOriginal") or details.get("textDisplay") or "").strip()
            if not msg:
                continue
            direct = url + "&lc=" + cid
            out.append(PublicItem("YouTube评论", "评论 · " + str(s.get("title", ""))[:80],
                                  msg[:4000], direct,
                                  details.get("authorDisplayName", ""), details.get("publishedAt", ""),
                                  post_url=url, comment_id=str(cid), comment_url=direct,
                                  navigation_kind="direct",
                                  author_url=str(details.get("authorChannelUrl") or "") if str(details.get("authorChannelUrl") or "").startswith("https://www.youtube.com/") else "",
                                  metrics=clean_metrics({"like": details.get("likeCount"), "comment": thread.get("snippet", {}).get("totalReplyCount")})))
    return out


def fetch_x(query: str):
    token = os.getenv("X_BEARER_TOKEN")
    if not token:
        raise RuntimeError("未配置 X_BEARER_TOKEN")
    r = _get("https://api.x.com/2/tweets/search/recent", params={"query": query, "max_results": 100, "tweet.fields": "created_at,author_id,public_metrics"}, headers={"Authorization": f"Bearer {token}"})
    out = []
    for x in r.json().get("data", []):
        if not x.get("id"):
            continue
        pm = x.get("public_metrics") or {}
        out.append(PublicItem("X", "", x.get("text", ""), f"https://x.com/i/web/status/{x['id']}", str(x.get("author_id", "")), x.get("created_at", ""),
                              metrics=clean_metrics({"like": pm.get("like_count"), "repost": pm.get("retweet_count"),
                                                     "comment": pm.get("reply_count"), "quote": pm.get("quote_count"),
                                                     "collect": pm.get("bookmark_count")})))
    return out


def fetch_github(query: str):
    """Official public GitHub issues search; works unauthenticated with limited quota."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "LeadRadar/2.0"}
    if os.getenv("GITHUB_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_API_TOKEN"]
    r = _get("https://api.github.com/search/issues", params={"q": query, "sort": "created", "order": "desc", "per_page": 50}, headers=headers)
    return [PublicItem("GitHub", x.get("title", ""), x.get("body", "") or "", x.get("html_url", ""), (x.get("user") or {}).get("login", ""), x.get("created_at", ""),
                       author_url=str((x.get("user") or {}).get("html_url") or ""),
                       metrics=clean_metrics({"like": (x.get("reactions") or {}).get("total_count"), "comment": x.get("comments")}))
            for x in r.json().get("items", [])]


def fetch_hackernews(query: str):
    """Hacker News public search via Algolia community API."""
    r = _get("https://hn.algolia.com/api/v1/search_by_date", params={"query": query, "hitsPerPage": 50})
    out = []
    for x in r.json().get("hits", []):
        object_id = x.get("objectID")
        if not re.fullmatch(r"[0-9]{1,30}", str(object_id or "")):
            continue
        destination = f"https://news.ycombinator.com/item?id={object_id}"
        is_comment = bool(x.get("comment_text"))
        author = x.get("author") or ""
        out.append(PublicItem("Hacker News评论" if is_comment else "Hacker News",
                              x.get("title") or x.get("story_title") or ("讨论回复" if is_comment else ""),
                              _clean_markup(x.get("comment_text") or x.get("story_text") or ""),
                              destination, x.get("author") or "", x.get("created_at") or "",
                              comment_id=str(object_id) if is_comment else "",
                              comment_url=destination if is_comment else "",
                              navigation_kind="direct" if is_comment else "",
                              author_url=f"https://news.ycombinator.com/user?id={author}" if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", author) else "",
                              metrics=clean_metrics({"like": x.get("points"), "comment": x.get("num_comments")})))
    return out


def fetch_bluesky(query: str):
    """Public Bluesky AppView posts. Search may be rate-limited or require auth in some deployments."""
    r = _get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts", params={
        "q": query, "sort": "latest", "limit": 50})
    out = []
    for post in r.json().get("posts", []):
        uri = str(post.get("uri") or "")
        actor = post.get("author") or {}
        handle = str(actor.get("handle") or "")
        record = post.get("record") or {}
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", handle):
            continue
        parts = uri.split("/")
        if len(parts) < 5 or parts[0] != "at:" or parts[-2] != "app.bsky.feed.post":
            continue
        rkey = parts[-1]
        if not re.fullmatch(r"[a-zA-Z0-9._~-]+", rkey):
            continue
        author = actor.get("displayName") or handle
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        # A Bluesky reply is itself a public post with its own permalink.
        # Don't confuse a like/repost with a comment.
        is_reply = isinstance(record.get("reply"), dict) and bool((record["reply"].get("parent") or {}).get("uri"))
        out.append(PublicItem("Bluesky评论" if is_reply else "Bluesky",
                              "Bluesky公开回复" if is_reply else "Bluesky公开帖子", record.get("text") or "",
                              url, author, record.get("createdAt", ""),
                              comment_url=url if is_reply else "",
                              navigation_kind="direct" if is_reply else "",
                              author_url=f"https://bsky.app/profile/{handle}",
                              metrics=clean_metrics({"like": post.get("likeCount"), "repost": post.get("repostCount"),
                                                     "comment": post.get("replyCount"), "quote": post.get("quoteCount")})))
    return out


def fetch_reddit_with_comments(query: str, max_posts: int = 5, max_comments: int = 20):
    """Public Reddit submissions and top-level comments, when .json endpoints work."""
    posts = fetch_reddit_json(query)[:max_posts]
    out = list(posts)
    for post in posts:
        path = urlparse(post.url).path.rstrip("/") + ".json"
        # Only construct from trusted reddit path returned by search.
        if not re.fullmatch(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+/(?:[A-Za-z0-9_%-]+/?)?\.json", path):
            continue
        try:
            resp = _get("https://www.reddit.com" + path,
                        params={"limit": min(50, max_comments), "depth": 1, "sort": "new"},
                        headers={"User-Agent": "LeadRadar/3.0 (public comment research)"})
            threads = resp.json()
        except (requests.RequestException, ValueError):
            continue  # one blocked thread must not discard already-found posts
        if not isinstance(threads, list) or len(threads) < 2:
            continue
        for child in threads[1].get("data", {}).get("children", [])[:max_comments]:
            data = child.get("data") or {}
            if child.get("kind") != "t1" or not data.get("body"):
                continue
            relative = data.get("permalink")
            if not isinstance(relative, str) or not relative.startswith("/r/"):
                continue
            when = datetime.fromtimestamp(data["created_utc"], timezone.utc).isoformat() if data.get("created_utc") else ""
            direct = "https://www.reddit.com" + relative
            out.append(PublicItem("Reddit评论", "评论 · " + (post.title or "")[:75],
                                  data["body"], direct,
                                  data.get("author", ""), when, post_url=post.url,
                                  comment_id=str(data.get("id") or ""), comment_url=direct,
                                  navigation_kind="direct", author_url=_reddit_user(str(data.get("author") or "")),
                                  metrics=clean_metrics({"like": data.get("score")})))
    return out


def fetch_github_with_comments(query: str, max_issues: int = 5, max_comments: int = 15):
    """Official public GitHub issues and recent public comments (bounded)."""
    issues = fetch_github(query)[:max_issues]
    out = list(issues)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "LeadRadar/3.0"}
    if os.getenv("GITHUB_API_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_API_TOKEN"]
    for item in issues:
        path = urlparse(item.url).path
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[2] not in ("issues", "pull") or not parts[3].isdigit():
            continue
        org, repo, _, number = parts
        if not all(re.fullmatch(r"[a-zA-Z0-9_.-]+", x) for x in (org, repo)):
            continue
        try:
            r = _get(f"https://api.github.com/repos/{org}/{repo}/issues/{number}/comments",
                     params={"per_page": min(100, max_comments)}, headers=headers)
            comments = r.json()
        except (requests.RequestException, ValueError):
            continue  # rate limit on one issue keeps the issues already found
        for c in comments[:max_comments] if isinstance(comments, list) else []:
            if not c.get("html_url") or not c.get("body"):
                continue
            direct = c["html_url"]
            # These GitHub issue comments have an official html_url anchor.
            if not (urlparse(direct).scheme == "https" and
                    urlparse(direct).hostname == "github.com" and
                    urlparse(direct).username is None and urlparse(direct).password is None and
                    urlparse(direct).port in (None, 443) and
                    urlparse(direct).path == urlparse(item.url).path and
                    re.fullmatch(r"issuecomment-\d+", urlparse(direct).fragment)):
                continue
            out.append(PublicItem("GitHub评论", "评论 · " + (item.title or "")[:80],
                                  c["body"], direct,
                                  (c.get("user") or {}).get("login", ""), c.get("created_at", ""),
                                  post_url=item.url, comment_id=str(c.get("id") or ""),
                                  comment_url=direct, navigation_kind="direct",
                                  author_url=str((c.get("user") or {}).get("html_url") or ""),
                                  metrics=clean_metrics({"like": (c.get("reactions") or {}).get("total_count")})))
    return out


def _normalized_rows(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ValueError("授权接口必须返回 JSON 对象或数组")
    data = payload
    for _ in range(4):
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            break
        found = False
        for key in ("items", "results", "list", "records", "data"):
            if key in data:
                data = data[key]
                found = True
                break
        if not found:
            break
    if isinstance(data, list):
        return data
    raise ValueError("授权接口 JSON 中未找到 data/items/results/list/records 数组")


def _value_text(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("nickname") or value.get("username") or value.get("id") or "")
    return str(value or "")


# Origin allow-list, used for navigation *only*: a vendor-provided URL is not
# automatically a verified platform deep link.
COMMENT_HOSTS = {
    "抖音": ("douyin.com",), "小红书": ("xiaohongshu.com",),
    "快手": ("kuaishou.com",), "微博": ("weibo.com",),
    "知乎": ("zhihu.com", "zhuanlan.zhihu.com"),
    "哔哩哔哩": ("bilibili.com",), "TikTok": ("tiktok.com",),
    "Instagram": ("instagram.com",), "Facebook": ("facebook.com",),
    "LinkedIn": ("linkedin.com",), "Telegram": ("t.me",),
    "Threads": ("threads.net",), "Pinterest": ("pinterest.com",),
    "Quora": ("quora.com",), "Mastodon": (), "Bluesky": ("bsky.app",),
    "Product Hunt": ("producthunt.com",),
}


def _trusted_comment_link(source: str, post_url: str, comment_url: str) -> bool:
    """Only mark an explicit, platform-originating link direct when it has a
    comment-related route, query or fragment. No invented destination URLs.
    """
    try:
        post, link = urlparse(post_url), urlparse(comment_url)
        if any(p.scheme != 'https' or not p.hostname or p.username or p.password
               or p.port not in (None, 443) for p in (post, link)):
            return False
        hosts = COMMENT_HOSTS.get(source, ())
        if not hosts or not any(post.hostname == h or post.hostname.endswith('.'+h) for h in hosts):
            return False
        if link.hostname != post.hostname or link.path != post.path:
            return False
        from urllib.parse import parse_qs
        keys = {k.lower() for k in parse_qs(link.query)}
        return bool(keys.intersection({'comment_id','commentid','reply_id','replyid','lc'}) or
                    re.fullmatch(r'(?:comment[-_]|reply[-_]?|issuecomment-)\w{3,100}', link.fragment, re.I))
    except (ValueError, AttributeError):
        return False


def fetch_authorized_json(source: str, query: str, url_env: str, token_env: str = ""):
    """Connector for a user-authorized JSON provider. Does not impersonate native APIs."""
    url = os.getenv(url_env, "").strip()
    if not url:
        raise RuntimeError(f"未配置 {url_env}")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ValueError(f"{url_env} 必须是有效的 HTTP(S) 地址")
    headers = {"User-Agent": "LeadRadar/2.0"}
    if token_env and os.getenv(token_env):
        headers["Authorization"] = "Bearer " + os.environ[token_env]
    payload = _get(url, params={"q": query, "keyword": query}, headers=headers, timeout=30).json()
    out = []
    for d in _normalized_rows(payload)[:200]:
        if not isinstance(d, dict):
            continue
        text = " ".join(str(d.get(k) or "") for k in ("text", "content", "desc", "description", "title")).strip()
        link = _value_text(d.get("url") or d.get("share_url") or d.get("link"))
        kind = str(d.get("type") or d.get("item_type") or "").lower()
        is_comment = kind in ("comment", "reply", "评论", "回复") or d.get("is_comment") is True
        author = _value_text(d.get("author") or d.get("nickname") or d.get("username") or d.get("user_id"))
        published = _value_text(d.get("published_at") or d.get("create_time") or d.get("created_at"))
        title = _value_text(d.get("title") or d.get("desc") or text[:120])
        metrics = clean_metrics({
            "like": d.get("like_count", d.get("digg_count", d.get("likes"))),
            "collect": d.get("collect_count", d.get("favorite_count", d.get("collects"))),
            "comment": d.get("comment_count", d.get("comments_count")),
            "share": d.get("share_count", d.get("shares")),
            "view": d.get("view_count", d.get("play_count")),
        })
        profile = _value_text(d.get("author_url") or d.get("user_url") or d.get("profile_url"))
        profile = profile if urlparse(profile).scheme == "https" else ""
        if is_comment:
            post_url = _value_text(d.get("post_url") or d.get("parent_url") or link)
            provided = _value_text(d.get("comment_url") or d.get("permalink") or "")
            if not provided and link != post_url:
                provided = link
            valid_direct = _trusted_comment_link(source, post_url, provided)
            direct = provided if valid_direct else ""
            comment_id = _value_text(d.get("comment_id") or d.get("reply_id") or "")[:100]
            # No arbitrary provider URL is promoted to a comment deep-link.
            out.append(PublicItem(source + "评论", "评论 · " + title, text,
                                  direct or post_url, author, published, post_url=post_url,
                                  comment_id=comment_id, comment_url=direct,
                                  navigation_kind="direct" if valid_direct else "manual",
                                  author_url=profile, metrics=metrics))
        else:
            out.append(PublicItem(source, title, text, link, author, published, author_url=profile, metrics=metrics))
    return out




def fetch_google_news(query: str):
    """Search public Google News RSS; these are news/mentions, not verified buyers."""
    params = urlencode({"q": query, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-CN"})
    return fetch_rss("https://news.google.com/rss/search?" + params, source="Google News",
                     redirect_hosts=("news.google.com",))[:50]


def fetch_bing_news(query: str):
    """Search the public Bing News RSS feed, when available from user's network."""
    params = urlencode({"q": query, "format": "rss"})
    # Mainland China networks are redirected from www.bing.com to cn.bing.com.
    return fetch_rss("https://www.bing.com/news/search?" + params, source="Bing News",
                     redirect_hosts=("bing.com",))[:50]

PLATFORM_ENV = {
    "douyin": ("抖音", "DOUYIN_API_URL", "DOUYIN_API_TOKEN"),
    "xhs": ("小红书", "XHS_API_URL", "XHS_API_TOKEN"),
    "kuaishou": ("快手", "KUAISHOU_API_URL", "KUAISHOU_API_TOKEN"),
    "weibo": ("微博", "WEIBO_API_URL", "WEIBO_API_TOKEN"),
    "zhihu": ("知乎", "ZHIHU_API_URL", "ZHIHU_API_TOKEN"),
    "bilibili": ("哔哩哔哩", "BILIBILI_API_URL", "BILIBILI_API_TOKEN"),
    "tiktok": ("TikTok", "TIKTOK_API_URL", "TIKTOK_API_TOKEN"),
    "instagram": ("Instagram", "INSTAGRAM_API_URL", "INSTAGRAM_API_TOKEN"),
    "facebook": ("Facebook", "FACEBOOK_API_URL", "FACEBOOK_API_TOKEN"),
    "linkedin": ("LinkedIn", "LINKEDIN_API_URL", "LINKEDIN_API_TOKEN"),
    "telegram": ("Telegram", "TELEGRAM_API_URL", "TELEGRAM_API_TOKEN"),
    "threads": ("Threads", "THREADS_API_URL", "THREADS_API_TOKEN"),
    "pinterest": ("Pinterest", "PINTEREST_API_URL", "PINTEREST_API_TOKEN"),
    "quora": ("Quora", "QUORA_API_URL", "QUORA_API_TOKEN"),
    "mastodon": ("Mastodon", "MASTODON_API_URL", "MASTODON_API_TOKEN"),
    "bluesky": ("Bluesky", "BLUESKY_API_URL", "BLUESKY_API_TOKEN"),
    "producthunt": ("Product Hunt", "PRODUCTHUNT_API_URL", "PRODUCTHUNT_API_TOKEN"),
}


def fetch_platform(platform: str, query: str):
    name, url_env, token_env = PLATFORM_ENV[platform]
    return fetch_authorized_json(name, query, url_env, token_env)


def item_key(item):
    # Keep previous key scheme for backwards-compatible deduplication.
    return hashlib.sha256((item.dedup_token or item.url or item.title + item.text).encode("utf-8")).hexdigest()
