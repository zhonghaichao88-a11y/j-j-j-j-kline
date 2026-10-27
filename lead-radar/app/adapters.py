"""Public and authorized content adapters. No login/CAPTCHA bypassing."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse, urlencode, urljoin
import hashlib
import html
import os
import re
import time
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
    # Inline highlight tags (<em>/<font>/<b>...) vanish without a space: Chinese search results
    # wrap the matched word in them ("医院<font>客服</font>系统" -> "医院客服系统").
    s = re.sub(r"</?(?:em|b|strong|font|span|i|u|mark|a)(?:\s[^>]*)?>", "", str(s or ""), flags=re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s))).strip()


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


_REDDIT_TOKEN = {"value": "", "expires": 0.0}


def _reddit_api():
    """(base URL, headers). Reddit blocks most anonymous .json requests (403) since 2023-2024;
    with REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET (a free "script" app at reddit.com/prefs/apps)
    the official application-only OAuth endpoint is used instead."""
    agent = os.getenv("REDDIT_USER_AGENT", "").strip() or "LeadRadar/4.0 (keyword monitor)"
    cid, secret = os.getenv("REDDIT_CLIENT_ID", "").strip(), os.getenv("REDDIT_CLIENT_SECRET", "").strip()
    if not (cid and secret):
        return "https://www.reddit.com", {"User-Agent": agent}, ".json"
    if not _REDDIT_TOKEN["value"] or _REDDIT_TOKEN["expires"] < time.time() + 60:
        r = requests.post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
                          data={"grant_type": "client_credentials"}, headers={"User-Agent": agent},
                          timeout=20, allow_redirects=False)
        if r.status_code in (400, 401, 403):
            raise RuntimeError("Reddit 授权失败：请检查 .env 里的 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET")
        r.raise_for_status()
        body = r.json()
        _REDDIT_TOKEN.update(value=str(body.get("access_token") or ""),
                             expires=time.time() + float(body.get("expires_in") or 3600))
        if not _REDDIT_TOKEN["value"]:
            raise RuntimeError("Reddit 没有返回访问令牌")
    return "https://oauth.reddit.com", {"User-Agent": agent, "Authorization": "Bearer " + _REDDIT_TOKEN["value"]}, ""


def fetch_reddit_json(query: str):
    base, headers, suffix = _reddit_api()
    try:
        r = _get(base + "/search" + suffix, params={"q": query, "sort": "new", "limit": 50, "raw_json": 1},
                 headers=headers)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403, 429) and suffix:
            raise RuntimeError("Reddit 拒绝了未登录的访问（%s）。到 reddit.com/prefs/apps 免费创建一个 script 应用，"
                               "把 client id / secret 填进 .env 的 REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET 后重启"
                               % exc.response.status_code) from exc
        raise
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


BLUESKY_HOSTS = ("api.bsky.app", "public.api.bsky.app")


def fetch_bluesky(query: str):
    """Public Bluesky AppView posts. Search may be rate-limited or require auth in some deployments."""
    params = {"q": query, "sort": "latest", "limit": 50}
    # public.api.bsky.app (the documented cached AppView) answers searchPosts with 403 since
    # 2025; api.bsky.app still serves it anonymously. Try both, newest working host first.
    last = None
    for host in BLUESKY_HOSTS:
        try:
            r = _get(f"https://{host}/xrpc/app.bsky.feed.searchPosts", params=params)
            break
        except requests.HTTPError as exc:
            last = exc
            if exc.response is None or exc.response.status_code not in (401, 403, 404):
                raise
    else:
        raise RuntimeError("Bluesky 搜索接口拒绝匿名访问，请稍后再试") from last
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
        path = urlparse(post.url).path.rstrip("/")
        # Only construct from trusted reddit path returned by search.
        if not re.fullmatch(r"/r/[A-Za-z0-9_]+/comments/[A-Za-z0-9]+(?:/[A-Za-z0-9_%-]+)?", path):
            continue
        try:
            base, headers, suffix = _reddit_api()
            resp = _get(base + path + suffix,
                        params={"limit": min(50, max_comments), "depth": 1, "sort": "new", "raw_json": 1},
                        headers=headers)
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


# ---------------------------------------------------------------- 4.0: more public sources
# Every source below was checked to answer anonymously (2026-10). Sources may still rate
# limit or change; failures are reported per source instead of being hidden.

def _ts(value):
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _csv_env(name, default=""):
    return [x.strip() for x in os.getenv(name, default).replace("，", ",").split(",") if x.strip()]


_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


def _clean_host(raw):
    """'https://forum.example.com/' -> 'forum.example.com'; '' for anything that isn't a plain host."""
    host = re.sub(r"^https?://", "", str(raw or "").strip().lower()).split("/", 1)[0]
    return host if _HOST_RE.fullmatch(host) else ""


STACKEXCHANGE_DOMAINS = {"stackoverflow": "stackoverflow.com", "superuser": "superuser.com",
                         "serverfault": "serverfault.com", "askubuntu": "askubuntu.com"}


def fetch_stackexchange(query: str, max_items: int = 30):
    """Questions on Stack Overflow & other Stack Exchange sites (official API, keyless: 300
    requests/day per IP; STACKEXCHANGE_KEY raises it to 10,000). STACKEXCHANGE_SITES picks sites."""
    sites = [s for s in _csv_env("STACKEXCHANGE_SITES", "stackoverflow,softwarerecs,webapps")
             if re.fullmatch(r"[a-z0-9.-]{2,40}", s)][:5] or ["stackoverflow"]
    out, errors = [], []
    for site in sites:
        params = {"order": "desc", "sort": "creation", "q": query, "site": site,
                  "pagesize": min(50, max(1, max_items)), "filter": "withbody"}
        if os.getenv("STACKEXCHANGE_KEY"):
            params["key"] = os.environ["STACKEXCHANGE_KEY"]
        try:
            body = _get("https://api.stackexchange.com/2.3/search/advanced", params=params).json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{site}: {str(exc)[:120]}")
            continue
        if body.get("error_message"):
            errors.append(f"{site}: {body['error_message'][:120]}")
            continue
        label = "Stack Overflow" if site == "stackoverflow" else "StackExchange·" + site
        for q in body.get("items", []):
            link = str(q.get("link") or "")
            if not link.startswith("https://"):
                continue
            owner = q.get("owner") or {}
            profile = str(owner.get("link") or "")
            out.append(PublicItem(label, _clean_markup(q.get("title")), _clean_markup(q.get("body"))[:6000], link,
                                  _clean_markup(owner.get("display_name")), _ts(q.get("creation_date")),
                                  author_url=profile if profile.startswith("https://") else "",
                                  metrics=clean_metrics({"like": q.get("score"), "comment": q.get("answer_count"),
                                                         "view": q.get("view_count")})))
    if not out and errors:
        raise RuntimeError("Stack Exchange 请求失败：" + "；".join(errors))
    return out


def fetch_lemmy(query: str, max_items: int = 30):
    """Lemmy (federated Reddit alternative): posts and comments via any instance's /api/v3/search."""
    instance = _clean_host(os.getenv("LEMMY_INSTANCE", "lemmy.world")) or "lemmy.world"
    body = _get(f"https://{instance}/api/v3/search", params={
        "q": query, "type_": "All", "sort": "New", "limit": min(50, max(1, max_items))}).json()
    out = []
    for row in body.get("posts", []):
        post, creator, counts = row.get("post") or {}, row.get("creator") or {}, row.get("counts") or {}
        url = str(post.get("ap_id") or "")
        if not url.startswith("https://"):
            continue
        out.append(PublicItem("Lemmy", post.get("name") or "", _clean_markup(post.get("body"))[:6000], url,
                              creator.get("display_name") or creator.get("name") or "", post.get("published") or "",
                              author_url=str(creator.get("actor_id") or "") if str(creator.get("actor_id") or "").startswith("https://") else "",
                              metrics=clean_metrics({"like": counts.get("score"), "comment": counts.get("comments")})))
    for row in body.get("comments", []):
        comment, post, creator = row.get("comment") or {}, row.get("post") or {}, row.get("creator") or {}
        url, post_url = str(comment.get("ap_id") or ""), str(post.get("ap_id") or "")
        if not (url.startswith("https://") and post_url.startswith("https://")):
            continue
        out.append(PublicItem("Lemmy评论", "评论 · " + str(post.get("name") or "")[:80],
                              _clean_markup(comment.get("content"))[:4000], url,
                              creator.get("display_name") or creator.get("name") or "", comment.get("published") or "",
                              post_url=post_url, comment_id=str(comment.get("id") or ""), comment_url=url,
                              navigation_kind="direct",
                              author_url=str(creator.get("actor_id") or "") if str(creator.get("actor_id") or "").startswith("https://") else "",
                              metrics=clean_metrics({"like": (row.get("counts") or {}).get("score")})))
    return out


def fetch_mastodon(query: str, max_items: int = 40):
    """Mastodon. Without a token only hashtag timelines are public, so the keyword is used as
    a hashtag (spaces removed). MASTODON_ACCESS_TOKEN enables real full-text status search."""
    instance = _clean_host(os.getenv("MASTODON_INSTANCE", "mastodon.social")) or "mastodon.social"
    token = os.getenv("MASTODON_ACCESS_TOKEN", "").strip()
    if token:
        body = _get(f"https://{instance}/api/v2/search", params={
            "q": query, "type": "statuses", "limit": min(40, max_items), "resolve": "false"},
            headers={"Authorization": "Bearer " + token, "User-Agent": "LeadRadar/4.0"}).json()
        statuses = body.get("statuses", []) if isinstance(body, dict) else []
    else:
        tag = re.sub(r"[^\w]", "", query, flags=re.UNICODE)
        if not tag:
            raise RuntimeError("Mastodon 未配置令牌时只能按话题标签搜索，关键词需要是字母/数字/汉字")
        statuses = _get(f"https://{instance}/api/v1/timelines/tag/{requests.utils.quote(tag)}",
                        params={"limit": min(40, max_items)}).json()
    out = []
    for st in statuses if isinstance(statuses, list) else []:
        url, account = str(st.get("url") or st.get("uri") or ""), st.get("account") or {}
        if not url.startswith("https://"):
            continue
        is_reply = bool(st.get("in_reply_to_id"))
        text = _clean_markup(st.get("content"))
        out.append(PublicItem("Mastodon评论" if is_reply else "Mastodon", text[:90], text, url,
                              account.get("display_name") or account.get("acct") or "", st.get("created_at") or "",
                              comment_url=url if is_reply else "", navigation_kind="direct" if is_reply else "",
                              author_url=str(account.get("url") or "") if str(account.get("url") or "").startswith("https://") else "",
                              metrics=clean_metrics({"like": st.get("favourites_count"), "repost": st.get("reblogs_count"),
                                                     "comment": st.get("replies_count")})))
    return out


def fetch_v2ex(query: str, max_items: int = 30):
    """V2EX (Chinese developer / indie-founder forum) via the public SOV2EX full-text search."""
    body = _get("https://www.sov2ex.com/api/search", params={
        "q": query, "size": min(50, max(1, max_items)), "sort": "created", "order": 0}).json()
    out = []
    for hit in body.get("hits", []):
        src = hit.get("_source") or {}
        tid, member = str(src.get("id") or ""), str(src.get("member") or "")
        if not tid.isdigit():
            continue
        created = str(src.get("created") or "")
        if created and not created.endswith("Z") and "+" not in created:
            created += "+08:00"   # sov2ex returns Beijing time without an offset
        out.append(PublicItem("V2EX", _clean_markup(src.get("title")), _clean_markup(src.get("content"))[:6000],
                              f"https://www.v2ex.com/t/{tid}", member, created,
                              author_url=f"https://www.v2ex.com/member/{member}" if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", member) else "",
                              metrics=clean_metrics({"comment": src.get("replies")})))
    return out


def fetch_discourse(query: str, max_items: int = 30):
    """Any public Discourse forum (DISCOURSE_SITES=forum.a.com,forum.b.com): newest matching posts."""
    sites = [h for h in (_clean_host(x) for x in _csv_env("DISCOURSE_SITES")) if h][:8]
    if not sites:
        raise RuntimeError("未配置 DISCOURSE_SITES：在 .env 填写要监控的 Discourse 论坛域名，逗号隔开")
    out, errors = [], []
    for site in sites:
        try:
            body = _get(f"https://{site}/search.json", params={"q": query + " order:latest"},
                        headers={"Accept": "application/json", "User-Agent": "LeadRadar/4.0"}).json()
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{site}: {str(exc)[:120]}")
            continue
        topics = {t.get("id"): t for t in body.get("topics", []) if isinstance(t, dict)}
        for post in body.get("posts", [])[:max_items]:
            topic = topics.get(post.get("topic_id")) or {}
            slug, tid, number = str(topic.get("slug") or "topic"), post.get("topic_id"), post.get("post_number") or 1
            if not str(tid).isdigit() or not re.fullmatch(r"[\w-]{1,200}", slug):
                continue
            url = f"https://{site}/t/{slug}/{tid}/{number}"
            user = str(post.get("username") or "")
            is_reply = int(number or 1) > 1
            out.append(PublicItem(f"Discourse·{site}" + ("评论" if is_reply else ""), _clean_markup(topic.get("title")),
                                  _clean_markup(post.get("blurb")), url, post.get("name") or user, post.get("created_at") or "",
                                  post_url=f"https://{site}/t/{slug}/{tid}" if is_reply else "",
                                  comment_url=url if is_reply else "", navigation_kind="direct" if is_reply else "",
                                  author_url=f"https://{site}/u/{user}" if re.fullmatch(r"[\w.-]{1,60}", user) else "",
                                  metrics=clean_metrics({"like": post.get("like_count")})))
    if not out and errors:
        raise RuntimeError("Discourse 请求失败：" + "；".join(errors))
    return out


_CCGP_ITEM = re.compile(r'<li>\s*<a href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>\s*(?:<p>(?P<summary>.*?)</p>)?\s*<span>(?P<meta>.*?)</span>', re.S)


def fetch_ccgp(query: str, days: int = 30):
    """中国政府采购网 tender / procurement notices (公开招标、询价、竞争性磋商、采购意向...).
    A public notice of an organisation that is about to buy is the strongest B2B signal there is.
    The site throttles frequent visits ("访问过于频繁"); keep monitoring intervals long."""
    end = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(end.timestamp() - days * 86400, timezone.utc)
    r = _get("https://search.ccgp.gov.cn/bxsearch", params={
        "searchtype": 1, "page_index": 1, "bidSort": 0, "pinMu": 0, "bidType": 0, "dbselect": "bidx",
        "kw": query, "start_time": start.strftime("%Y:%m:%d"), "end_time": end.strftime("%Y:%m:%d"),
        "timeType": 6, "pppStatus": 0}, headers={"User-Agent": "Mozilla/5.0 LeadRadar/4.0"}, timeout=25,
        redirect_hosts=("ccgp.gov.cn",))
    page = r.content.decode("utf-8", "replace")
    if "访问过于频繁" in page or "频繁访问" in page:
        raise RuntimeError("中国政府采购网提示访问过于频繁，请过一段时间再试（监控间隔建议 3 小时以上）")
    block = page.split('vT-srch-result-list-bid', 1)[-1].split('vT-srch-result-page', 1)[0]
    out = []
    for m in _CCGP_ITEM.finditer(block):
        url = urljoin("https://www.ccgp.gov.cn/", html.unescape(m.group("url")).strip())
        if (urlparse(url).hostname or "").rsplit(".", 3)[-3:] != ["ccgp", "gov", "cn"]:
            continue
        meta = _clean_markup(m.group("meta"))
        buyer = re.search(r"采购人[:：]\s*([^|｜]+)", meta)
        when = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", meta)
        published = (f"{when.group(1)}-{int(when.group(2)):02d}-{int(when.group(3)):02d}T"
                     f"{int(when.group(4) or 0):02d}:{when.group(5) or '00'}:00+08:00") if when else ""
        title = _clean_markup(m.group("title"))
        out.append(PublicItem("政府采购", title, (_clean_markup(m.group("summary") or "") + "\n" + meta).strip(),
                              url, buyer.group(1).strip() if buyer else "", published))
    return out
