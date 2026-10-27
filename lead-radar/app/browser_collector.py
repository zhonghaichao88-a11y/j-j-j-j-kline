"""Visible-page keyword and comment collection using an explicitly user-controlled browser.

Does not call private platform APIs, copy browser cookies, defeat login/CAPTCHA,
or promise full-site completeness. Intended for bounded public-content research.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse, parse_qs

from .adapters import PublicItem, clean_metrics

SUPPORTED = {
    "xhs": "小红书", "douyin": "抖音", "weibo": "微博", "zhihu": "知乎",
    "bilibili": "哔哩哔哩", "kuaishou": "快手", "tiktok": "TikTok", "tieba": "百度贴吧",
}
LABEL_TO_PLATFORM = {label: key for key, label in SUPPORTED.items()}
# Conservative collection limits; no unbounded scrolling or automated bulk harvesting.
MAX_POSTS = 20
MAX_COMMENTS_PER_POST = 30

ALLOWED_HOSTS = {
    "xhs": ("xiaohongshu.com",), "douyin": ("douyin.com",),
    "weibo": ("weibo.com",), "zhihu": ("zhihu.com",),
    "bilibili": ("bilibili.com",), "kuaishou": ("kuaishou.com",),
    "tiktok": ("tiktok.com",), "tieba": ("tieba.baidu.com",),
}
POST_PATTERNS = {
    "xhs": [r"/explore/[a-zA-Z0-9]+", r"/search_result/[a-zA-Z0-9]+", r"/discovery/item/[a-zA-Z0-9]+"],
    "douyin": [r"/video/\d+", r"/note/\d+"],
    "weibo": [r"/\d+/[a-zA-Z0-9]+", r"/status/[a-zA-Z0-9]+"],
    "zhihu": [r"/question/\d+(?:/answer/\d+)?", r"/p/\d+"],
    "bilibili": [r"/video/BV[a-zA-Z0-9]+"],
    "kuaishou": [r"/short-video/[a-zA-Z0-9_-]+"],
    "tiktok": [r"/@[a-zA-Z0-9._-]+/video/\d+"],
    "tieba": [r"/p/\d+"],
}
PROFILE_PATTERNS = {
    "xhs": [r"/user/profile/[a-zA-Z0-9]+"],
    "douyin": [r"/user/[A-Za-z0-9_-]{6,}"],
    "weibo": [r"/u/\d+", r"/\d{5,}", r"/n/[^/]+"],
    "zhihu": [r"/people/[A-Za-z0-9_-]+", r"/org/[A-Za-z0-9_-]+"],
    "bilibili": [r"/\d+"],                      # space.bilibili.com/<mid>
    "kuaishou": [r"/profile/[A-Za-z0-9_-]+"],
    "tiktok": [r"/@[a-zA-Z0-9._-]+"],
    "tieba": [r"/home/main"],
}
# Some sites (seen on bilibili) prefix paths with a UI language: /zh-Hans/video/BV...
LOCALE_PREFIX = re.compile(r"^/(?:zh-hans|zh-hant|zh-cn|zh-tw|zh-hk|en|en-us|ja|ko)(?=/)", re.I)


def _strip_locale(path: str) -> str:
    return LOCALE_PREFIX.sub("", path or "")


# ---------------------------------------------------------------- page scripts
# Shared helpers. deepAll() also walks open shadow roots (bilibili's comment
# web components keep every comment inside nested shadow DOM).
HELPERS_JS = r"""
  const deepAll = (root, sel) => {
    const out = [];
    const walk = r => {
      try { out.push(...r.querySelectorAll(sel)); } catch (e) {}
      for (const el of r.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
    };
    walk(root);
    return out;
  };
  const visible = el => !!(el && el.getClientRects().length);
  const txt = el => (el ? (el.innerText || el.textContent || '') : '').trim();
  const PROFILE_SEL = 'a[href*="/user/profile/"], a[href*="/user/"], a[href*="space.bilibili.com"], a[href*="/u/"], a[href*="/n/"], a[href*="/people/"], a[href*="/org/"], a[href*="/profile/"], a[href*="/@"], a[href*="/home/main"]';
  const COMMENT_SELECTORS = __COMMENT_SELECTORS__;
  const TEXT_SEL = '.note-text, .content, [class*="comment-content"], [class*="commentContent"], [data-e2e="comment-text"], .CommentContent, .CommentItem-content, .comment-text, .reply-content, .lzl_content_main, .d_post_content, #content';
  const AUTHOR_SEL = '.author .name, .author-wrapper .name, .author, [class*="author-name"], [class*="user-name"], [data-e2e="comment-username"], .CommentItemV2-meta .UserLink-link, .UserLink-link, .user-name, .name, .lzl_p_p, .p_author_name';
  // Returns {text, author, authorHref, id, href, ctime, like} for one comment row.
  const rowInfo = (platform, el) => {
    const tag = el.tagName.toLowerCase();
    if (tag === 'bili-comment-renderer' || tag === 'bili-comment-reply-renderer') {
      const sr = el.shadowRoot;
      if (!sr) return null;
      const rich = sr.querySelector('bili-rich-text');
      const text = txt(rich?.shadowRoot?.querySelector('#contents') || rich);
      const info = sr.querySelector('bili-comment-user-info')?.shadowRoot;
      const nameEl = info?.querySelector('#user-name');
      const link = info?.querySelector('#user-name a') || sr.querySelector('#user-avatar');
      const d = el.data || el.__data || {};
      return {text, author: txt(nameEl), authorHref: link?.href || '', id: String(d.rpid_str || d.rpid || ''),
              href: '', ctime: Number(d.ctime || 0) || 0, like: d.like ?? null};
    }
    const textEl = el.querySelector(TEXT_SEL) || el;
    const authorEl = el.querySelector(AUTHOR_SEL);
    const profile = el.querySelector(PROFILE_SEL);
    let id = el.getAttribute('data-comment-id') || el.getAttribute('data-reply-id') || el.getAttribute('comment_id') || el.getAttribute('data-id') || '';
    if (!id && /^comment-[A-Za-z0-9_-]+$/.test(el.id || '')) id = el.id.slice(8);
    const href = el.querySelector('a[href*="comment_id="], a[href*="reply_id="], a[href*="/comment/"], a[href*="#reply"], a[href*="#comment-"]')?.href || '';
    const likeEl = el.querySelector('.like .count, .like-wrapper .count, [class*="like"] [class*="count"], [data-e2e="comment-like-count"]');
    return {text: txt(textEl), author: txt(authorEl) || txt(profile), authorHref: profile?.href || '',
            id: String(id).slice(0, 100), href, ctime: 0, like: likeEl ? txt(likeEl) : null};
  };
"""

# Only DOM nodes visible in a browser controlled by the user. These selectors
# are best-effort and may change after platform updates. No private endpoints.
COMMENT_SELECTORS_JS = r"""{
  xhs: ['.comment-item', '[class*="commentItem"]', '[class*="CommentItem"]'],
  douyin: ['[data-e2e="comment-item"]', '[class*="CommentItem_container"]', '[class*="comment-item"]'],
  weibo: ['[comment_id]', '[node-type="comment_list"] .list_li', '[class*="CommentList_commentItem"]', '.wbpro-list .item1'],
  zhihu: ['.CommentItem', '.NestComment', '[class*="CommentItem"]', '[data-id][class*="Comment"]'],
  bilibili: ['bili-comment-renderer', 'bili-comment-reply-renderer', '.reply-item', '.sub-reply-item'],
  kuaishou: ['[class*="comment-item"]', '[class*="CommentItem"]'],
  tiktok: ['[data-e2e="comment-level-1"]', '[data-e2e="comment-level-2"]', '[class*="DivCommentItemContainer"]'],
  tieba: ['.lzl_single_post', '.j_lzl_s_p', '.l_post', '[class*="comment_item"]']
}"""
HELPERS_JS = HELPERS_JS.replace("__COMMENT_SELECTORS__", COMMENT_SELECTORS_JS)

# DOM-only extraction; no signatures, interception or internal/unpublished APIs.
SEARCH_DOM_JS = r"""(platform) => {
  __HELPERS__
  const cards = {
    xhs: ['section.note-item', '.note-item'],
    douyin: ['[data-e2e="search-video-item"]', '[data-e2e="scroll-list"] li', '.search-result-card'],
    weibo: ['.card-wrap[mid]', '.card-wrap'],
    zhihu: ['.SearchResult-Card', '.List-item'],
    bilibili: ['.bili-video-card', '.video-list-item'],
    kuaishou: ['.video-card', '[class*="video-card"]'],
    tiktok: ['[data-e2e="search_top-item"]', '[data-e2e="search-common-link"]', '[class*="DivItemContainer"]'],
    tieba: ['.s_post']
  };
  const anchors = {
    xhs: ['a[href*="/search_result/"]', 'a[href*="/explore/"]', 'a[href*="/discovery/item/"]'],
    douyin: ['a[href*="/video/"]', 'a[href*="/note/"]'],
    weibo: ['a[href*="weibo.com/"][href*="/"]', 'a[href*="/status/"]'],
    zhihu: ['a[href*="/question/"]', 'a[href*="zhuanlan.zhihu.com/p/"]'],
    bilibili: ['a[href*="/video/BV"]'],
    kuaishou: ['a[href*="/short-video/"]'],
    tiktok: ['a[href*="/video/"]'],
    tieba: ['a[href*="/p/"]']
  };
  const titles = {
    xhs: ['.footer .title span', '.footer .title', '.title span', '.title'],
    douyin: ['[class*="video-title"]', '[class*="title"]', '[class*="desc"]'],
    weibo: ['p.txt[node-type="feed_list_content_full"]', 'p.txt', '.txt'],
    zhihu: ['.ContentItem-title', 'h2'],
    bilibili: ['.bili-video-card__info--tit', 'h3[title]', 'h3'],
    kuaishou: ['.video-info-title', '[class*="title"]'],
    tiktok: ['[data-e2e="search-card-video-caption"]', '[data-e2e="search-card-desc"]', '[class*="Caption"]'],
    tieba: ['.p_title a', '.p_title']
  };
  const firstVisible = (root, sels) => {
    for (const sel of sels) for (const el of root.querySelectorAll(sel)) if (visible(el) && txt(el)) return el;
    return null;
  };
  const postSel = (anchors[platform] || []).join(',');
  const out = [];
  const pushCard = (card, links) => {
    // Prefer a visible link carrying xsec_token (xiaohongshu requires it); hidden
    // tokenless /explore/ links usually open "笔记无法浏览".
    links.sort((a, b) => (b.href.includes('xsec_token') - a.href.includes('xsec_token')) || (visible(b) - visible(a)));
    const link = links[0];
    if (!link) return;
    const urls = [...new Set(links.map(l => l.href))].slice(0, 12);  // Python picks the first valid work URL
    const text = txt(card).slice(0, 2500);
    // Only visible title nodes: bilibili cards carry a hidden "反馈成功" [class*=title] element.
    const titleEl = firstVisible(card, [...(titles[platform] || []), '.title', '[class*="title"]', '[class*="desc"]', 'h3', 'h2']);
    const authorEl = card.querySelector('.author .name, .author-wrapper .name, .author, [class*="author-name"], [class*="user-name"], .up-name, .bili-video-card__info--author, .name');
    const profile = card.querySelector(PROFILE_SEL);
    const likeEl = card.querySelector('.like-wrapper .count, [class*="like"] [class*="count"], [data-e2e="video-like-count"]');
    out.push({url: link.href, urls, title: (titleEl?.getAttribute('title') || txt(titleEl) || link.getAttribute('title') || text.slice(0, 90)).slice(0, 400),
              text, author: (txt(authorEl) || txt(profile)).slice(0, 160), authorHref: profile?.href || '',
              like: likeEl ? txt(likeEl) : null});
  };
  const cardNodes = (cards[platform] || []).flatMap(s => [...document.querySelectorAll(s)]);
  for (const card of cardNodes) pushCard(card, [...card.querySelectorAll(postSel)]);
  if (!out.length) {
    for (const link of document.querySelectorAll(postSel)) {
      const card = link.closest('article, li, section, [class*="card"], [class*="item"]') || link;
      pushCard(card, [link]);
    }
  }
  return out;
}""".replace("__HELPERS__", HELPERS_JS)

COMMENTS_DOM_JS = r"""(platform) => {
  __HELPERS__
  const selector = (COMMENT_SELECTORS[platform] || []).join(',');
  const els = selector ? deepAll(document, selector) : [];
  const out = [], seen = new Set();
  for (const el of els) {
    if (!visible(el)) continue;
    const row = rowInfo(platform, el);
    if (!row) continue;
    row.text = row.text.slice(0, 1400); row.author = row.author.slice(0, 160);
    const key = row.author + '\u0000' + row.text;
    if (row.text.length < 2 || seen.has(key)) continue;
    seen.add(key);
    out.push(row);
  }
  return out;
}""".replace("__HELPERS__", HELPERS_JS)

# Public counters on a work's detail page. First selector that yields a number wins.
METRICS_DOM_JS = r"""(platform) => {
  __HELPERS__
  const S = {
    xhs: {like: ['.engage-bar .like-wrapper .count', '.interact-container .like-wrapper .count', '.interactions .like-wrapper .count'],
          collect: ['.engage-bar .collect-wrapper .count', '.interact-container .collect-wrapper .count', '#note-page-collect-board-guide .count'],
          comment: ['.engage-bar .chat-wrapper .count', '.interact-container .chat-wrapper .count', '.comments-container .total'],
          author: ['.author-wrapper .username', '.author-container .username', '.author .name']},
    douyin: {like: ['[data-e2e="video-player-digg"]', '[data-e2e="detail-video-info"] [data-e2e="video-player-digg"]'],
             collect: ['[data-e2e="video-player-collect"]'], comment: ['[data-e2e="feed-comment-icon"]', '[data-e2e="video-player-comment"]'],
             share: ['[data-e2e="video-player-share"]'], author: ['[data-e2e="video-detail"] [data-e2e="user-info"] a', '[data-e2e="user-info"] a']},
    weibo: {like: ['.woo-like-count', '[class*="toolbar_like"] [class*="num"]', '[action-type="feed_list_like"] em'],
            comment: ['[class*="toolbar_comment"] [class*="num"]', '[action-type="feed_list_comment"] em'],
            share: ['[class*="toolbar_retweet"] [class*="num"]', '[action-type="feed_list_forward"] em'],
            author: ['[class*="head_name"]', '.head_nick', '.name']},
    zhihu: {like: ['.VoteButton--up', '.VoteButton'], comment: ['.ContentItem-actions button[class*="Comment"]', '.QuestionHeader-Comment button'],
            author: ['.AuthorInfo-name .UserLink-link', '.AuthorInfo-name']},
    bilibili: {like: ['.video-like-info'], coin: ['.video-coin-info'], collect: ['.video-fav-info'], share: ['.video-share-info-text', '.video-share-info'],
               view: ['.view-text', '.video-info-detail .view'], comment: ['.total-reply'], author: ['.up-name', '.up-detail-top a']},
    kuaishou: {like: ['[class*="like-count"]', '.like-icon + span', '[class*="like"] [class*="count"]'], comment: ['[class*="comment-count"]'],
               author: ['.profile-user-name-title', '[class*="author-name"]', '.user-name']},
    tiktok: {like: ['[data-e2e="like-count"]', '[data-e2e="browse-like-count"]'], comment: ['[data-e2e="comment-count"]', '[data-e2e="browse-comment-count"]'],
             collect: ['[data-e2e="undefined-count"]', '[data-e2e="collect-count"]'], share: ['[data-e2e="share-count"]'],
             author: ['[data-e2e="browse-username"]', '[data-e2e="video-author-uniqueid"]']},
    tieba: {comment: ['.l_reply_num .red', '.l_reply_num span'], author: ['.p_author_name', '.d_name a']}
  };
  const PUBLISHED = {
    xhs: ['.note-content .date', '.bottom-container .date', '.date'],
    douyin: ['[data-e2e="detail-video-publish-time"]', '[class*="publish-time"]'],
    weibo: ['[class*="head-info_time"]', '.from a'],
    zhihu: ['.ContentItem-time', '.QuestionPage .ContentItem-time'],
    bilibili: ['.pubdate-ip-text', '.pubdate-text', '.pubdate'],
    kuaishou: ['[class*="publish-time"]', '.video-info-time'],
    tiktok: ['[data-e2e="browser-nickname"] span:last-child'],
    tieba: ['.post-tail-wrap .tail-info:last-child', '.p_tail li:last-child']
  };
  const conf = S[platform] || {};
  const out = {};
  for (const [key, sels] of Object.entries(conf)) {
    for (const sel of sels) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const value = txt(el);
      if (key === 'author') {
        const a = el.matches('a[href]') ? el : (el.querySelector('a[href]') || el.closest('a[href]'));
        out.author = value.slice(0, 160); out.authorHref = a?.href || '';
        break;
      }
      if (/\d/.test(value)) { out[key] = value.slice(0, 40); break; }
    }
  }
  for (const sel of PUBLISHED[platform] || []) {
    const el = document.querySelector(sel);
    const value = txt(el).replace(/^(发布于|编辑于|发表于)\s*/, '');
    if (value && /\d/.test(value)) { out.published = value.slice(0, 60); break; }
  }
  if (!out.authorHref) {
    const a = document.querySelector('.author-wrapper a[href*="/user/profile/"], [data-e2e="user-info"] a[href*="/user/"], a.up-name, .AuthorInfo a[href*="/people/"]');
    if (a) { out.authorHref = a.href; out.author = out.author || txt(a).slice(0, 160); }
  }
  return out;
}""".replace("__HELPERS__", HELPERS_JS)

# Login walls / human-verification pages are detected and reported, never solved.
BLOCKED_DOM_JS = r"""() => {
  const t = (document.title || '') + ' ' + location.href;
  if (/验证码|安全验证|人机验证|captcha|verify|unhuman|passport\.|\/login|signin|sso\//i.test(t)) return 'verify-or-login-page';
  const sel = '.login-container, .login-modal, #login-panel-new, [class*="login-mask"], [class*="LoginModal"], .passport-login-container, .Modal-content .SignFlow, #captcha_container, .captcha_verify_container, .geetest_panel, .nc_wrapper, iframe[src*="captcha"], iframe[src*="verify"]';
  for (const el of document.querySelectorAll(sel)) if (el.getClientRects().length) return 'login-or-captcha-dialog';
  return '';
}"""

LOCATE_DOM_JS = r"""([platform, targetText, targetAuthor, targetId]) => {
  __HELPERS__
  const selector = (COMMENT_SELECTORS[platform] || []).join(',');
  const norm = s => String(s || '').replace(/\s+/g,' ').trim().toLowerCase();
  const wanted = norm(targetText).slice(0, 240);
  const authorWanted = norm(targetAuthor);
  const idWanted = String(targetId || '').trim();
  if (wanted.length < 2 && !idWanted) return {found:false};
  for (const el of deepAll(document, selector)) {
    if (!visible(el)) continue;
    const row = rowInfo(platform, el);
    if (!row) continue;
    const text = norm(row.text);
    const author = norm(row.author);
    const sameId = !!(idWanted && row.id && idWanted === row.id);
    const contentMatches = wanted.length >= 2 && (text.includes(wanted) || (wanted.length >= 35 && text.includes(wanted.slice(0,80))));
    if (sameId || (contentMatches && (!authorWanted || !author || author.includes(authorWanted)))) {
      el.scrollIntoView({block:'center'});
      el.style.outline='4px solid #df501e';
      el.style.outlineOffset='6px';
      el.style.backgroundColor='#fff5bb';
      return {found:true,method:sameId?'id':'text'};
    }
  }
  return {found:false};
}""".replace("__HELPERS__", HELPERS_JS)

# Scroll the actual comments pane where possible (e.g. video modal), not only
# the window. Fallback to Playwright mouse wheel for sites with dynamic scrollers.
SCROLL_COMMENTS_JS = r"""(platform) => {
  const selectors = {
    xhs: ['.note-scroller', '.comments-container', '[class*="comment-container"]'],
    douyin: ['[data-e2e="comment-list"]', '[class*="CommentList"]', '[data-e2e="detail-video-info"] ~ div'],
    weibo: ['[node-type="comment_list"]', '.wbpro-list'],
    zhihu: ['.CommentListV2', '.CommentList', '.Modal-content'],
    bilibili: ['bili-comments', '.reply-list'],
    kuaishou: ['[class*="comment-list"]', '[class*="comment-wrapper"]'],
    tiktok: ['[data-e2e="comment-list"]','[class*="DivCommentListContainer"]', '[class*="DivCommentContainer"]'],
    tieba: ['.lzl_cnt', '.p_postlist']
  };
  const all = (selectors[platform] || []).flatMap(sel => [...document.querySelectorAll(sel)]);
  const candidates = [...all, ...document.querySelectorAll('[class*="comment"], [class*="Comment"]')];
  for (const el of candidates) {
    for (let node = el; node && node !== document.body; node = node.parentElement) {
      if (!node.getClientRects().length || node.scrollHeight <= node.clientHeight + 30) continue;
      const overflow = getComputedStyle(node).overflowY;
      if (overflow !== 'auto' && overflow !== 'scroll') continue;
      const before = node.scrollTop;
      node.scrollTop += Math.max(300, Math.floor(node.clientHeight * .85));
      if (node.scrollTop !== before) return true;
      break;
    }
  }
  return false;
}"""


# ---------------------------------------------------------------- URL helpers
def search_url(platform: str, keyword: str, sort: str = "latest") -> str:
    """Search page URL. sort='latest' asks for newest-first where the site accepts a URL parameter."""
    q = quote(keyword)
    latest = sort == "latest"
    if platform == "xhs":  # newest-first is a UI filter here; see _apply_latest_sort
        return "https://www.xiaohongshu.com/search_result?keyword=" + q + "&source=web_explore_feed"
    if platform == "douyin":
        return "https://www.douyin.com/search/" + q + ("?publish_time=0&sort_type=2&type=video" if latest else "")
    if platform == "weibo":
        return ("https://s.weibo.com/realtime?q=" + q + "&rd=realtime&tw=realtime&Refer=weibo_realtime") if latest \
            else "https://s.weibo.com/weibo?q=" + q
    if platform == "zhihu":
        return "https://www.zhihu.com/search?type=content&q=" + q
    if platform == "bilibili":
        return "https://search.bilibili.com/all?keyword=" + q + ("&order=pubdate" if latest else "")
    if platform == "kuaishou":
        return "https://www.kuaishou.com/search/video?searchKey=" + q
    if platform == "tiktok":
        return "https://www.tiktok.com/search?q=" + q
    if platform == "tieba":
        return "https://tieba.baidu.com/f/search/res?ie=utf-8&qw=" + q + ("&sm=1" if latest else "")
    raise ValueError("不支持的平台")


def _host_ok(platform: str, host: str) -> bool:
    return platform in ALLOWED_HOSTS and any(host == x or host.endswith("." + x) for x in ALLOWED_HOSTS[platform])


def safe_post_url(platform: str, candidate: str) -> bool:
    try:
        p = urlparse(candidate)
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return False
        if not _host_ok(platform, host):
            return False
        path = _strip_locale(p.path)
        return any(re.fullmatch(pattern + "/?", path) for pattern in POST_PATTERNS[platform])
    except (ValueError, AttributeError):
        return False


def canonical_post_url(platform: str, url: str) -> str:
    """Stable identity of a work for de-duplication: no query, fragment, locale or
    trailing slash. xiaohongshu's per-session xsec_token must not create new rows."""
    p = urlparse(url)
    path = _strip_locale(p.path).rstrip("/")
    host = (p.hostname or "").lower()
    if platform == "xhs":
        m = re.search(r"/(?:explore|search_result|discovery/item)/([a-zA-Z0-9]+)", path)
        if m:
            return "https://www.xiaohongshu.com/explore/" + m.group(1)
    return f"https://{host}{path}"


def safe_profile_url(platform: str, candidate: str) -> str:
    """Return a cleaned public profile URL for the platform, or ''."""
    try:
        p = urlparse(str(candidate or "").strip())
        host = (p.hostname or "").lower()
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
            return ""
        if platform == "bilibili":
            if host != "space.bilibili.com":
                return ""
        elif not _host_ok(platform, host):
            return ""
        path = _strip_locale(p.path).rstrip("/") or "/"
        if not any(re.fullmatch(pattern, path) for pattern in PROFILE_PATTERNS.get(platform, [])):
            return ""
        if platform == "tieba":  # profile identity lives in the query (?id=...)
            ident = parse_qs(p.query).get("id", [""])[0] or parse_qs(p.query).get("un", [""])[0]
            return f"https://tieba.baidu.com/home/main?id={quote(ident)}" if ident else ""
        keep = ("?" + p.query) if platform == "xhs" and "xsec_token" in p.query else ""
        return f"https://{host}{path}{keep}"[:600]
    except (ValueError, AttributeError):
        return ""


def comment_item(platform: str, post_url: str, post_title: str, comment: dict) -> PublicItem | None:
    text = str(comment.get("text") or "").strip()
    author = str(comment.get("author") or "").strip()[:160]
    if len(text) < 2 or not safe_post_url(platform, post_url):
        return None
    marker = hashlib.sha256((author + "\0" + text).encode("utf-8")).hexdigest()[:20]
    post_link = post_url.split("#", 1)[0]
    raw_id = str(comment.get("id") or "").strip()
    comment_id = raw_id if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", raw_id) else ""
    # Real comment URL only when the page exposes a literal permalink on the
    # same trusted platform. Never manufacture #lr-comment links for navigation.
    provided_link = str(comment.get("href") or "").strip()
    if not provided_link and platform == "bilibili" and comment_id.isdigit():
        # bilibili's own reply anchor (used by its notification centre): #reply<rpid>
        provided_link = post_link + "#reply" + comment_id
    if not safe_comment_permalink(platform, post_link, provided_link):
        provided_link = ""
    published = ""
    try:
        ctime = int(comment.get("ctime") or 0)
        if ctime > 1_000_000_000:
            published = datetime.fromtimestamp(ctime, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return PublicItem(SUPPORTED[platform] + "评论", "评论 · " + post_title[:85], text[:1400],
                      provided_link or post_link, author, published,
                      post_url=post_link, comment_id=comment_id, comment_url=provided_link,
                      navigation_kind="direct" if provided_link else "assisted",
                      dedup_token=canonical_post_url(platform, post_link) + "#lr-comment-" + marker,
                      author_url=safe_profile_url(platform, comment.get("authorHref") or ""),
                      metrics=clean_metrics({"like": comment.get("like")}))


def safe_comment_permalink(platform: str, post_url: str, candidate: str) -> bool:
    if not candidate or not safe_post_url(platform, post_url):
        return False
    try:
        post = urlparse(post_url)
        link = urlparse(candidate)
        if link.scheme != "https" or link.username or link.password or link.port not in (None, 443):
            return False
        if link.hostname != post.hostname:
            return False
        # Comment anchor must refer to this post rather than to a different work.
        if _strip_locale(link.path).rstrip("/") != _strip_locale(post.path).rstrip("/"):
            return False
        parameters = {key.lower() for key in parse_qs(link.query).keys()}
        if parameters.intersection({"comment_id", "commentid", "reply_id", "replyid"}):
            return True
        # Accept only a literal fragment from a site-rendered anchor, never our
        # old local #lr-comment marker; the platform controls final routing.
        return bool(re.fullmatch(r"(?:comment[-_]|reply[-_]?)[A-Za-z0-9_-]{3,100}", link.fragment, re.I))
    except ValueError:
        return False


# ---------------------------------------------------------------- browser flow
NO_BROWSER_MSG = "缺少浏览器采集组件：请关闭程序后重新双击 start.bat，它会自动安装"


# Domestic platforms (and their CDNs) are reached directly even when a system proxy /
# VPN is on: a "global" proxy routing douyin.com abroad typically ends in ERR_CONNECTION_CLOSED.
DIRECT_DOMAINS = [
    "douyin.com", "douyinpic.com", "douyinstatic.com", "douyinvod.com", "iesdouyin.com", "amemv.com",
    "bytedance.com", "byteimg.com", "bytescm.com", "bytetos.com", "snssdk.com", "zjcdn.com", "ibytedtos.com",
    "xiaohongshu.com", "xhscdn.com", "xhslink.com",
    "weibo.com", "weibo.cn", "sinaimg.cn", "sina.com.cn", "sinajs.cn",
    "zhihu.com", "zhimg.com",
    "bilibili.com", "hdslb.com", "bilivideo.com", "biliapi.net", "biliimg.com", "b23.tv",
    "kuaishou.com", "kuaishouzt.com", "yximgs.com", "gifshow.com", "ksapisrv.com",
    "baidu.com", "bdstatic.com", "bdimg.com", "baidupcs.com", "bcebos.com",
    "qq.com", "gtimg.cn", "alicdn.com", "aliyuncs.com",
]


def _system_proxy() -> str:
    """The Windows/system proxy (Clash, v2rayN... "system proxy" switch) as host:port URL, or ''."""
    try:
        import urllib.request
        proxies = urllib.request.getproxies()
    except Exception:
        return ""
    for key, scheme in (("http", "http"), ("https", "http"), ("socks", "socks5")):
        raw = proxies.get(key) or ""
        if not raw:
            continue
        # Windows "ProxyServer=127.0.0.1:7890" is reported as https://127.0.0.1:7890 for the
        # https key, but the proxy itself still speaks plain HTTP.
        hostport = re.sub(r"^[a-z0-9]+://", "", raw.strip(), flags=re.I).rstrip("/")
        if hostport:
            return f"{scheme}://{hostport}"
    return ""


def proxy_settings(route: str | None = None):
    """(launch options, human description). `route` (or BROWSER_PROXY in .env):
    auto (default) = system proxy for foreign sites, domestic platforms direct;
    system = follow the system proxy for everything; direct = never use a proxy;
    or an explicit proxy such as http://127.0.0.1:7890."""
    mode = (route or os.getenv("BROWSER_PROXY", "auto")).strip()
    low = mode.lower()
    if low == "relay":
        from .relay import relay_url
        # Everything through the built-in channel: it resolves names with public DNS itself.
        return {"proxy": {"server": relay_url(), "bypass": "<-loopback>"}}, ROUTE_LABELS["relay"]
    if low in ("direct", "off", "none", "no", "0"):
        return {"args": ["--no-proxy-server"]}, "不使用代理"
    if low == "system":
        return {}, "跟随系统代理"
    server = _system_proxy() if low in ("", "auto") else mode
    if not server:
        return {}, "未检测到系统代理"
    bypass = ",".join(DIRECT_DOMAINS + ["." + d for d in DIRECT_DOMAINS] + ["localhost", "127.0.0.1"])
    return {"proxy": {"server": server, "bypass": bypass}}, f"检测到代理 {server}：国内平台直连，境外网站走代理"


NET_HINTS = [
    (("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET", "ERR_EMPTY_RESPONSE", "ERR_SSL_PROTOCOL_ERROR"),
     "连接被中断。多半是代理/VPN 造成的：把代理软件切到「规则模式」或暂时关闭后重试；"
     "如果代理软件开了 TUN/虚拟网卡模式，请关掉它（浏览器设置管不到这一层）"),
    (("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_NO_SUPPORTED_PROXIES"),
     "代理连不上：代理软件可能已经关了但系统代理还开着。请打开代理软件，或在 Windows 设置里关闭系统代理；"
     "也可以在 .env 里设置 BROWSER_PROXY=direct"),
    (("ERR_NAME_NOT_RESOLVED", "ERR_NAME_RESOLUTION_FAILED"), "域名解析失败：请检查网络或 DNS 设置"),
    (("ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED", "ERR_ADDRESS_UNREACHABLE"), "电脑没有联网或网络刚切换，请检查网络后重试"),
    (("ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT", "Timeout"), "打开超时：网络太慢，或代理节点不通。请稍后重试或切换代理模式"),
    (("ERR_CERT_",), "证书错误：可能是代理/安全软件在拦截 HTTPS，请检查代理或杀毒软件设置"),
]


def friendly_net_error(exc) -> str:
    text = str(exc)
    for codes, hint in NET_HINTS:
        hit = next((c for c in codes if c in text), None)
        if hit:
            code = re.search(r"net::(ERR_[A-Z_]+)", text)
            return f"{hint}（{code.group(1) if code else hit}）"
    return f"{type(exc).__name__}：{text.splitlines()[0][:180] if text else ''}"


def _launch(pw, profile_dir: Path, headless: bool = False, route: str | None = None,
            channel: str | None = None, **kwargs):
    """Persistent browser profile. Uses Playwright's Chromium when installed and
    falls back to the Edge/Chrome already on the computer (Edge ships with Windows),
    so a failed Chromium download doesn't block collection. BROWSER_CHANNEL=msedge|chrome forces one.
    `channel` launches exactly that browser; `route` picks the proxy handling (see proxy_settings)."""
    proxy_opts, _ = proxy_settings(route)
    extra_args = list(kwargs.pop("args", [])) + list(proxy_opts.get("args", []))
    options = dict(user_data_dir=str(profile_dir), headless=headless,
                   viewport={"width": 1330, "height": 880}, locale="zh-CN", **kwargs)
    if extra_args:
        options["args"] = extra_args
    if "proxy" in proxy_opts:
        options["proxy"] = proxy_opts["proxy"]
    forced = channel or os.getenv("BROWSER_CHANNEL", "").strip()
    channels = [forced] if forced else [None, "msedge", "chrome"]
    last_error = None
    for channel in channels:
        try:
            return pw.chromium.launch_persistent_context(**options, **({"channel": channel} if channel else {}))
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            missing = ("executable doesn't exist" in text or "is not found" in text or
                       "not installed" in text or "no such file" in text)
            if not missing:
                # e.g. the profile is still open in another collector window
                raise RuntimeError("浏览器无法启动：如果另一个采集/登录/定位浏览器窗口还开着，请先关闭它。"
                                   + str(exc).splitlines()[0][:160]) from exc
    raise RuntimeError(NO_BROWSER_MSG + "（也没有找到 Edge/Chrome）") from last_error


ROUTE_LABELS = {"auto": "国内平台直连、境外网站走代理", "direct": "完全不走代理", "system": "全部走系统代理",
                "relay": "程序内置直连通道（用公共 DNS，绕开系统 DNS 和代理）"}


def system_dns_is_fake(host: str = "www.douyin.com") -> bool:
    """True when the system resolver answers with VPN fake-IP addresses (198.18.x.x)."""
    from .relay import is_fake
    try:
        return any(is_fake(r[4][0]) for r in socket.getaddrinfo(host, 443, socket.AF_INET))
    except OSError:
        return False


def _route_file(profile_dir: Path) -> Path:
    return profile_dir.parent / "network_route.json"


def _profile_for(profile_dir: Path, channel: str | None) -> Path:
    # The system Edge gets its own profile folder: sharing one with Chromium can corrupt it.
    return profile_dir if not channel else profile_dir.with_name(profile_dir.name + "_" + channel)


def _route_candidates(profile_dir: Path):
    """Ways to reach the platforms, best guess first: the combination that worked last time,
    then domestic-direct, no proxy, everything via the system proxy; then the same with the
    system Edge (some security tools / accelerators only let well-known browsers through)."""
    env = os.getenv("BROWSER_PROXY", "auto").strip().lower()
    if env not in ("", "auto"):
        routes = [None]                         # explicit .env setting is respected as-is
    elif _system_proxy():
        routes = ["auto", "direct", "system", "relay"]
    else:
        routes = ["auto", "direct", "relay"]
    if "relay" in routes and system_dns_is_fake():
        # DNS still hands out fake addresses (VPN TUN left-overs): only the channel can work
        routes.remove("relay")
        routes.insert(0, "relay")
    forced = os.getenv("BROWSER_CHANNEL", "").strip()
    channels = [forced or None] if forced else [None, "msedge"]
    combos = [(r, c) for c in channels for r in routes]
    try:
        import json
        saved = json.loads(_route_file(profile_dir).read_text(encoding="utf-8"))
        best = (saved.get("route"), saved.get("channel"))
        if best in combos:
            combos.remove(best)
            combos.insert(0, best)
    except (OSError, ValueError, AttributeError):
        pass
    return combos


def _remember_route(profile_dir: Path, route, channel):
    try:
        import json
        _route_file(profile_dir).write_text(json.dumps({"route": route, "channel": channel}), encoding="utf-8")
    except OSError:
        pass


def describe_route(route, channel) -> str:
    text = ROUTE_LABELS.get(route or "", "按 .env 的 BROWSER_PROXY 设置")
    return text + ("（使用电脑自带的 Edge）" if channel == "msedge" else "")


def _is_network_error(exc) -> bool:
    text = str(exc)
    return "net::ERR_" in text or "Timeout" in text


def _launch_reachable(pw, profile_dir: Path, test_urls, headless: bool = False,
                      note: Callable[[str], None] | None = None, **kwargs):
    """Launch the collector browser with a network route that can open *every* site in
    `test_urls` (one URL per selected platform: xiaohongshu opening fine says nothing about
    douyin). Only *network* errors such as ERR_CONNECTION_CLOSED count as failures; login
    walls and captchas do not. If no route opens all of them, the route that opened the most
    is used and the sites that still fail are reported.
    Returns (context, route description)."""
    urls = [test_urls] if isinstance(test_urls, str) else list(dict.fromkeys(test_urls))
    host = lambda u: urlparse(u).hostname or u
    best = None            # (opened count, route, channel, failing hosts)
    tried = []
    for route, channel in _route_candidates(profile_dir):
        try:
            ctx = _launch(pw, _profile_for(profile_dir, channel), headless, route=route, channel=channel, **kwargs)
        except RuntimeError:
            if channel:          # e.g. Edge not installed: just skip this option
                continue
            raise
        desc = describe_route(route, channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        failing = []
        for url in urls:
            try:
                page.goto(url, wait_until="commit", timeout=15000)
            except Exception as exc:
                if _is_network_error(exc):
                    failing.append(f"{host(url)}（{friendly_net_error(exc).split('（')[-1].rstrip('）')}）")
        if not failing:
            _remember_route(profile_dir, route, channel)
            if tried and note:
                note(f"已自动切换网络方式：{desc}（之前的方式：{'；'.join(tried)}）")
            return ctx, desc
        tried.append(f"{desc} → 打不开 {'、'.join(failing)}")
        opened = len(urls) - len(failing)
        if best is None or opened > best[0]:
            best = (opened, route, channel, failing)
        if note:
            note(f"「{desc}」打不开 {'、'.join(f.split('（')[0] for f in failing)}，正在自动换一种网络方式重试…")
        try:
            ctx.close()
        except Exception:
            pass
    if best is None:
        raise RuntimeError(NO_BROWSER_MSG)
    opened, route, channel, failing = best
    ctx = _launch(pw, _profile_for(profile_dir, channel), headless, route=route, channel=channel, **kwargs)
    if opened:
        _remember_route(profile_dir, route, channel)
    if note:
        note("没有一种网络方式能打开全部平台，已选用打开最多的：" + describe_route(route, channel) +
             "。仍打不开：" + "、".join(failing) + "。请点「🩺 网络诊断」并把结果截图发给技术人员；"
             "也请用电脑上平时用的浏览器打开这些网址，看能不能打开。")
    return ctx, describe_route(route, channel)


def _test_url_for(platforms) -> list:
    """One page per selected platform, domestic ones first."""
    ordered = [p for p in platforms if p != "tiktok"] + [p for p in platforms if p == "tiktok"]
    return [LOGIN_PAGES[p] for p in ordered if p in LOGIN_PAGES]


_JUNK_LINE = re.compile(r"^[\d\s.:：,万wW+·/-]*$|不支持\s*HTML5|请更换浏览器|反馈成功")


def _clean_card_text(text: str) -> str:
    """Drop view/duration counters and player notices that card innerText carries."""
    lines = [line.strip() for line in str(text or "").splitlines()]
    return "\n".join(line for line in lines if line and not _JUNK_LINE.search(line))[:2500]


def _visible_rows(page, js: str, *args):
    try:
        result = page.evaluate(js, *args)
        return result if isinstance(result, list) else []
    except Exception:
        return []


def _blocked(page) -> str:
    try:
        return str(page.evaluate(BLOCKED_DOM_JS) or "")
    except Exception:
        return ""


def _scroll_comments(page, platform: str):
    """Scroll the comment pane; the wheel fallback is aimed at the page centre
    (Playwright's mouse otherwise sits at 0,0 outside any scroll container)."""
    try:
        moved = page.evaluate(SCROLL_COMMENTS_JS, platform)
    except Exception:
        moved = False
    if not moved:
        try:
            size = page.viewport_size or {"width": 1330, "height": 880}
            page.mouse.move(int(size["width"] * 0.62), int(size["height"] * 0.6))
            page.mouse.wheel(0, 900)
        except Exception:
            pass


def _apply_latest_sort(page, platform: str):
    """Best effort: xiaohongshu has no URL parameter for newest-first; click its
    visible filter like a user would. Silently keeps default order if absent."""
    if platform != "xhs":
        return
    try:
        page.locator(".filter, .filter-box").or_(page.get_by_text("筛选", exact=True)).first.click(timeout=2500)
        page.wait_for_timeout(600)
        page.get_by_text("最新", exact=True).first.click(timeout=2500)
        page.wait_for_timeout(2200)
        page.keyboard.press("Escape")
    except Exception:
        pass


def _wait_until_usable(page, platform, label, on_event, wait_seconds, headless, probe):
    """Poll `probe()` until it returns rows. If a login/verification wall shows,
    tell the operator (visible mode) and wait for a human to finish it."""
    rows = probe()
    if rows:
        return rows, ""
    deadline_rounds = max(1, int(wait_seconds) // 3)
    told = False
    for _ in range(deadline_rounds):
        reason = _blocked(page)
        if reason:
            if headless:
                return [], "平台显示登录/验证页面，无头模式无法处理。请在 .env 设 MONITOR_HEADLESS=0 并在弹出的浏览器里登录一次。"
            if not told:
                on_event(platform, "waiting_login", [],
                         f"{label}需要登录或安全验证，请在弹出的浏览器窗口手动完成（最多等待约 {int(wait_seconds)} 秒）。")
                told = True
        page.wait_for_timeout(3000)
        rows = probe()
        if rows:
            return rows, ""
    reason = _blocked(page)
    if reason:
        return [], f"{label}停留在登录/验证页面，未完成验证，本轮跳过。"
    return [], ""


def locate_visible_comment(platform: str, post_url: str, comment_text: str, author: str,
                           profile_dir: Path, on_status: Callable[[str, str], None],
                           max_scrolls: int = 20, window_seconds: int = 120, comment_id: str = ""):
    """User-initiated highlight in their own visible browser, not a direct permalink.

    Stops on sign-in/challenge or absent content; doesn't bypass restrictions.
    """
    if not safe_post_url(platform, post_url):
        raise ValueError("作品链接不属于支持的平台")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(NO_BROWSER_MSG) from exc
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        origin = "https://" + (urlparse(post_url).hostname or "") + "/"
        ctx, _ = _launch_reachable(pw, profile_dir, origin, note=lambda m: on_status("running", m))
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(post_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            if _blocked(page):
                on_status("running", "页面要求登录或验证，请在弹出的浏览器中完成，完成后会继续查找…")
                for _ in range(40):
                    if page.is_closed() or not _blocked(page):
                        break
                    page.wait_for_timeout(1500)
            for i in range(max_scrolls + 1):
                if page.is_closed():
                    on_status("not_found", "窗口已关闭，未能定位评论")
                    return
                try:
                    found = page.evaluate(LOCATE_DOM_JS, [platform, comment_text, author, comment_id])
                except Exception:
                    found = False
                if isinstance(found, dict) and found.get("found"):
                    on_status("located", "已匹配评论" + ("ID" if found.get("method") == "id" else "文字") + "并高亮；可以查看或回复")
                    # Keep browser open for user to interact; finish on close or timeout.
                    for _ in range(max(1, int(window_seconds))):
                        if page.is_closed():
                            break
                        page.wait_for_timeout(1000)
                    return
                if i != max_scrolls:
                    _scroll_comments(page, platform)
                    page.wait_for_timeout(1000)
            on_status("not_found", "没有在已加载的评论中找到原文；可能已折叠、排序变化或需要登录。请在已打开的作品页手动查看")
            for _ in range(30):
                if page.is_closed():
                    break
                page.wait_for_timeout(1000)
        finally:
            ctx.close()


def collect_in_browser(keyword: str, platforms: list[str], profile_dir: Path,
                       on_event: Callable[[str, str, list[PublicItem], str], None],
                       max_posts: int = 8, comments_per_post: int = 10,
                       headless: bool = False, login_wait_seconds: int = 60,
                       sort: str = "latest"):
    """Open the user's browser profile. Caller owns storage, status & authorization policy.

    on_event(platform, state, new_items, message) is invoked after each batch.
    A user may need to sign in manually in the browser window. If blocked,
    the crawler reports it and moves on; it never attempts to bypass the check.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(NO_BROWSER_MSG) from exc
    max_posts = min(MAX_POSTS, max(1, int(max_posts)))
    comments_per_post = min(MAX_COMMENTS_PER_POST, max(0, int(comments_per_post)))
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        context, route_desc = _launch_reachable(
            pw, profile_dir, _test_url_for(platforms), headless,
            note=lambda m: on_event(platforms[0], "searching", [], m), args=["--disable-notifications"])
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for platform in platforms:
                label = SUPPORTED[platform]
                on_event(platform, "searching", [], f"正在打开{label}搜索页（{'最新优先' if sort == 'latest' else '综合排序'}）。")
                try:
                    page.goto(search_url(platform, keyword, sort), wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(2500)

                    def probe():
                        found = {}
                        for row in _visible_rows(page, SEARCH_DOM_JS, platform):
                            candidates = [str(u) for u in (row.get("urls") or [row.get("url")]) if u]
                            url = next((u for u in candidates if safe_post_url(platform, u)), "")
                            if not url:
                                continue
                            row["url"] = url
                            key = canonical_post_url(platform, url)
                            old = found.get(key)
                            if old is None or ("xsec_token" in url and "xsec_token" not in old["url"]):
                                found[key] = row
                        return list(found.values())

                    posts, blocked_msg = _wait_until_usable(page, platform, label, on_event,
                                                            login_wait_seconds, headless, probe)
                    if blocked_msg:
                        on_event(platform, "error", [], blocked_msg)
                        continue
                    if not posts and sort == "latest" and search_url(platform, keyword, "latest") != search_url(platform, keyword, "relevance"):
                        # Newest-first parameter not honoured by this site version: fall back.
                        page.goto(search_url(platform, keyword, "relevance"), wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        posts, blocked_msg = _wait_until_usable(page, platform, label, on_event, 15, headless, probe)
                        if blocked_msg:
                            on_event(platform, "error", [], blocked_msg)
                            continue
                    if posts and sort == "latest":
                        _apply_latest_sort(page, platform)
                        posts = probe() or posts
                    # A couple of page scrolls to reach max_posts; still bounded.
                    for _ in range(4):
                        if len(posts) >= max_posts:
                            break
                        page.mouse.move(665, 500)
                        page.mouse.wheel(0, 1600)
                        page.wait_for_timeout(1500)
                        merged = {canonical_post_url(platform, r["url"]): r for r in posts}
                        for row in probe():
                            merged.setdefault(canonical_post_url(platform, row["url"]), row)
                        posts = list(merged.values())
                    if not posts:
                        on_event(platform, "empty", [], "页面未提供可读取的搜索结果。可能需要登录、验证、调整关键词，或平台已改变页面结构。不会伪造数据。")
                        continue
                    unique = posts[:max_posts]
                    on_event(platform, "reading", [], f"发现 {len(unique)} 条可见作品，正在依次读取作品、互动数据和评论。")
                    total_comments = 0
                    for idx, post in enumerate(unique, start=1):
                        title = str(post.get("title") or "").strip()[:400] or "未命名作品"
                        summary = _clean_card_text(post.get("text") or "")
                        url = post["url"]
                        author = str(post.get("author") or "")[:160]
                        author_url = safe_profile_url(platform, post.get("authorHref") or "")
                        metrics = clean_metrics({"like": post.get("like")})
                        published = ""
                        comments = []
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2200)
                            if _blocked(page) and not headless:
                                on_event(platform, "waiting_login", [], f"{label}作品页需要登录/验证，请在浏览器中完成。")
                                for _ in range(max(1, int(login_wait_seconds) // 3)):
                                    if not _blocked(page):
                                        break
                                    page.wait_for_timeout(3000)
                            try:
                                detail = page.evaluate(METRICS_DOM_JS, platform) or {}
                            except Exception:
                                detail = {}
                            metrics.update(clean_metrics(detail))
                            published = str(detail.get("published") or "")[:60]
                            author = author or str(detail.get("author") or "")[:160]
                            author_url = author_url or safe_profile_url(platform, detail.get("authorHref") or "")
                            if comments_per_post:
                                # Accumulate while scrolling: comment lists are often virtualised.
                                gathered = {}
                                for _ in range(8):
                                    for row in _visible_rows(page, COMMENTS_DOM_JS, platform):
                                        gathered.setdefault((row.get("author"), row.get("text")), row)
                                    if len(gathered) >= comments_per_post:
                                        break
                                    _scroll_comments(page, platform)
                                    page.wait_for_timeout(1100)
                                comments = list(gathered.values())[:comments_per_post]
                        except Exception as exc:
                            on_event(platform, "reading", [], f"第 {idx} 条作品的详情/评论不可读取：{type(exc).__name__}；继续处理其他作品。")
                        record = PublicItem(label + "作品", title, summary or title, url, author, published,
                                            author_url=author_url, metrics=metrics,
                                            dedup_token=canonical_post_url(platform, url))
                        batch = [record]
                        for entry in comments:
                            c = comment_item(platform, url, title, entry)
                            if c:
                                batch.append(c)
                                total_comments += 1
                        on_event(platform, "reading", batch, f"{label}已处理 {idx}/{len(unique)} 条作品，读取 {total_comments} 条可见评论。")
                        if idx < len(unique):
                            page.wait_for_timeout(1200)
                    on_event(platform, "done", [], f"{label}已完成：{len(unique)} 条作品，{total_comments} 条可见评论（不保证全量）。")
                except Exception as exc:
                    on_event(platform, "error", [], f"{label}打不开：{friendly_net_error(exc)}")
        finally:
            context.close()


LOGIN_PAGES = {
    "xhs": "https://www.xiaohongshu.com/explore", "douyin": "https://www.douyin.com/",
    "weibo": "https://weibo.com/", "zhihu": "https://www.zhihu.com/signin",
    "bilibili": "https://www.bilibili.com/", "kuaishou": "https://www.kuaishou.com/",
    "tiktok": "https://www.tiktok.com/login", "tieba": "https://tieba.baidu.com/",
}


def open_login_pages(platforms: list[str], profile_dir: Path, on_status: Callable[[str, str], None],
                     max_minutes: int = 20):
    """Open each platform's home/sign-in page in the collector's profile so the user signs
    in by hand (QR code, SMS...). Cookies stay in profile_dir for later searches."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(NO_BROWSER_MSG) from exc
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx, route_desc = _launch_reachable(pw, profile_dir, _test_url_for(platforms),
                                            note=lambda m: on_status("running", m))
        try:
            first = ctx.pages[0] if ctx.pages else ctx.new_page()
            failed = []
            for i, platform in enumerate(platforms):
                page = first if i == 0 else ctx.new_page()
                try:
                    page.goto(LOGIN_PAGES[platform], wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:  # leave the tab open; the user can retry inside the browser
                    failed.append(f"{SUPPORTED[platform]}打不开：{friendly_net_error(exc)}")
            if failed:
                on_status("running", "；".join(failed) + f"。当前网络方式：{route_desc}。处理后在浏览器里点「重新加载」即可，其他标签页可以照常登录")
            else:
                on_status("running", f"已打开 {len(platforms)} 个平台（网络方式：{route_desc}），请逐个标签页登录；全部完成后关闭浏览器窗口")
            for _ in range(max_minutes * 60):
                open_pages = [p for p in ctx.pages if not p.is_closed()]
                if not open_pages:
                    break
                open_pages[0].wait_for_timeout(1000)
            on_status("done", "登录窗口已关闭，登录状态已保存，之后的搜索和监控会自动使用")
        except Exception:
            # Closing the whole browser window ends the context; that's the normal finish.
            on_status("done", "登录窗口已关闭，登录状态已保存，之后的搜索和监控会自动使用")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
