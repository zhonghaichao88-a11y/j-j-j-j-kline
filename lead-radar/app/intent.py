"""Rule-based lead intent scoring (4.0).

Transparent and explainable: every score comes with the phrases that produced it, so an
operator can see *why* a post is ranked high and tune the word lists in the settings page.
It is a priority estimate, not a verified purchase probability.

Biggest change from 3.x: most keyword hits on social platforms are *sellers* advertising
(加微信 / 承接 / 招代理 ...), not buyers. Those are now detected and pushed down instead of
being ranked as high-intent leads.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

# Explicit "no need" overrides incidental positive words in the same text.
NEGATIVE = [
    r"(?:没有|无|暂无|不想|不需要|无需|没打算|不考虑|拒绝|并非|不是).{0,4}(?:购买|采购|求购|买单|买入|购入)(?:需求|计划|意愿)?",
    r"(?:没有|暂无|无|不需要).{0,3}(?:购买需求|采购需求|购买计划|采购计划)",
    r"(?:不求|无需|别|不要).{0,3}推荐",
    r"\b(?:not|never|no)\s+(?:looking\s+to\s+buy|buying|purchasing|interested\s+in\s+buying)\b",
    r"\bno\s+(?:purchase|buying)\s+(?:intent|need)\b",
]

# (pattern, label shown to the operator)
BUYER_STRONG = [
    (r"求购", "求购"), (r"求推荐|求安利|蹲(?:一个|个)?推荐", "求推荐"), (r"求报价|请报价|询价|报个价", "询价"),
    (r"采购", "采购"), (r"多少钱|什么价|价格(?:多少|怎么样|贵吗)|怎么收费|收费(?:标准|多少)|费用(?:多少|大概)", "问价格"),
    (r"哪家(?:好|靠谱|强|便宜)|哪个(?:牌子|品牌|平台|软件|工具)(?:好|靠谱)|有没有靠谱|靠谱的.{0,6}(?:吗|推荐)", "问哪家好"),
    (r"有(?:人|公司|团队|大佬|老师).{0,8}(?:接单|提供|做|能做|会做)(?:吗|嘛|么)?", "找人做"),
    (r"(?:找|寻找|物色).{0,10}(?:供应商|团队|服务商|厂家|公司|外包|师傅|合作方|解决方案|渠道)", "找供应商"),
    (r"(?:想|准备|打算|计划|需要).{0,3}(?:买|购买|入手|采购|上一套|做一个|找一个|找个|定制|开发)", "有购买计划"),
    (r"在哪(?:里)?(?:买|购买|下单|能买)|怎么(?:买|购买|下单|报名|联系)|求(?:链接|联系方式|店铺|购买方式)", "问怎么买"),
    (r"预算(?:\d|大概|有|在|是|充足|多少)", "有预算"),
    # English: "looking for" alone is every second developer question; require a product-ish noun.
    (r"\b(?:looking\s+for|need|searching\s+for|recommend(?:ation)?s?\s+for)\s+(?:a|an|some|good|the\s+best|any)?\s*"
     r"(?:[\w-]+\s+){0,4}(?:tool|software|service|platform|app|vendor|agency|provider|solution|crm|consultant|"
     r"freelancer|contractor|developer|company|supplier|saas)s?\b", "looking for"),
    (r"\b(?:want\s+to\s+buy|request\s+a\s+quote|where\s+can\s+i\s+buy|any\s+recommendations?|"
     r"(?:good|cheaper|better)\s+alternatives?\s+to|alternatives?\s+to\s+\w+\?|switching\s+from|"
     r"how\s+much\s+(?:does|do|would|for)|can\s+(?:anyone|someone)\s+recommend|willing\s+to\s+pay|hire\s+(?:a|an|someone))\b", "looking for"),
]
BUYER_WEAK = [
    (r"想了解|有没有|怎么做|怎么样|好用吗|值得(?:买|入手|用)吗?|求教|请教|咨询一下|问一下|有人知道|有人用过", "咨询/提问"),
    (r"比较|对比|试用|测评|接入|方案|选型|替代", "在比较方案"),
    (r"\b(?:how\s+to|anyone|trial|evaluating|considering|budget|which\s+(?:is|one))\b", "evaluating"),
]
# Short replies under a post that mean "contact me / send me the link" (小红书/抖音评论区常见).
COMMENT_BUYER = [(r"^(?:dd|滴滴|蹲|求|私|私我|想要|求带|已私|戳|我也要|同求|求分享)[!！。.~～\s]*$|同求|求私|私我|带我一个", "评论求联系")]

SELLER = [
    (r"欢迎(?:咨询|私信|来电|合作|洽谈|选购)|咨询(?:热线|电话)", "欢迎咨询"),
    (r"加(?:微|薇|威|v|V|vx|VX|wx|WX|微信|qq|QQ)|微信号|vx[:：]|wx[:：]|联系电话|手机号[:：]?\s*1\d{10}|电话[:：]\s*\d{6,}", "留联系方式"),
    (r"(?:诚|广)?招(?:代理|加盟|商)|招商加盟|代理招募|一件代发", "招商"),
    (r"(?:本公司|我司|我们公司|本工作室|我们团队|本人)(?:专业|专注|长期|提供|承接|主营)|专业(?:承接|定制|代做|代办|提供)|长期(?:承接|接单)|承接各类", "商家自我介绍"),
    (r"厂家直(?:销|供)|一手货源|量大从优|价格优惠|限时(?:优惠|特价|折扣)|优惠活动|包邮|官方旗舰|点击(?:链接|主页)|主页有|看主页", "促销广告"),
    (r"招聘|诚聘|急聘|岗位职责|任职要求|薪资|兼职", "招聘信息"),
    (r"深度解析|一次讲清|避坑(?:指南|清单)|(?:优先|首选)推荐|附(?:避坑|清单|教程)|(?:\d+|十)(?:款|个).{0,8}(?:横评|对比|测评)", "软文/测评文"),
    (r"\b(?:we\s+(?:offer|help|build|provide)|our\s+(?:team|agency|company|platform|solution|product)|dm\s+(?:me|us)|"
     r"book\s+a\s+(?:demo|call)|limited\s+offer|sign\s+up|get\s+started|free\s+trial|learn\s+more|contact\s+us|"
     r"link\s+in\s+bio|helps?\s+(?:businesses|teams|companies|brands|manufacturers|you)|check\s+out\s+our|demo\s+account)\b", "seller ad"),
    (r"(?:#[^\s#]+\s*){4,}|(?:#[^\s#]+.*){5,}", "话题标签堆砌"),
]


def _hits(rules, text, extra=()):
    found = []
    for pattern, label in list(rules) + [(re.escape(w), w) for w in extra if w]:
        if re.search(pattern, text, re.I) and label not in found:
            found.append(label)
    return found


def analyze(text: str, *, is_comment: bool = False, published_at: str = "",
            custom_buyer=(), custom_seller=()) -> dict:
    """Return {intent, score, lead_type, reasons}. lead_type: buyer / seller / question / other."""
    t = (text or "").casefold()
    if any(re.search(p, t) for p in NEGATIVE):
        return {"intent": "low", "score": 10, "lead_type": "other", "reasons": ["明确表示没有需求"]}
    strong = _hits(BUYER_STRONG, t, custom_buyer)
    weak = _hits(BUYER_WEAK, t)
    if is_comment:
        strong += [x for x in _hits(COMMENT_BUYER, t.strip()) if x not in strong]
    seller = _hits(SELLER, t, custom_seller)
    question = bool(re.search(r"[?？]|吗[。！!]?$|吗[，,]|么[?？]?$", t))

    if strong:
        intent, score, lead_type = "high", 80 + min(15, 5 * (len(strong) - 1)), "buyer"
    elif weak:
        intent, score, lead_type = "medium", 55 + (5 if question else 0) + min(10, 5 * (len(weak) - 1)), "question"
    else:
        intent, score, lead_type = "low", 25 if question else 20, "question" if question else "other"
    reasons = strong + weak
    if seller:
        # A seller ad that also says "推荐" / "多少钱" is still an ad.
        score -= 45 if not strong else 30
        if not strong or len(seller) >= len(strong):
            intent, lead_type = "low", "seller"
        elif intent == "high":
            intent = "medium"
        reasons += ["疑似商家/广告：" + s for s in seller]
    if question and "提问" not in reasons and lead_type != "seller":
        reasons.append("提问")
    age = _age_days(published_at)
    if age is not None and age <= 3 and lead_type != "seller":
        score += 5
        reasons.append("3 天内发布")
    return {"intent": intent, "score": max(0, min(100, score)), "lead_type": lead_type, "reasons": reasons[:8]}


def _age_days(published_at: str):
    if not published_at:
        return None
    try:
        when = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


# ---------------------------------------------------------------- relative times
_REL = [(r"(\d+)\s*秒前", 1), (r"(\d+)\s*分钟前", 60), (r"(\d+)\s*小时前", 3600), (r"(\d+)\s*天前", 86400),
        (r"(\d+)\s*周前", 7 * 86400), (r"(\d+)\s*(?:个)?月前", 30 * 86400)]
CN_TZ = timezone(timedelta(hours=8))


def parse_cn_time(raw: str, now: datetime | None = None) -> str:
    """'3小时前' / '昨天 12:30' / '10-21' / '2026-10-21 08:00' / '编辑于 2天前 · IP 北京' -> ISO 8601.
    Returns '' when the text has no recognisable time (never guesses)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    now = (now or datetime.now(timezone.utc)).astimezone(CN_TZ)
    if re.search(r"刚刚|just now", text, re.I):
        return now.isoformat(timespec="seconds")
    for pattern, seconds in _REL:
        m = re.search(pattern, text)
        if m:
            return (now - timedelta(seconds=int(m.group(1)) * seconds)).isoformat(timespec="seconds")
    day = None
    if "前天" in text:
        day = now - timedelta(days=2)
    elif "昨天" in text:
        day = now - timedelta(days=1)
    elif "今天" in text:
        day = now
    hm = re.search(r"(\d{1,2}):(\d{2})", text)
    if day is not None:
        h, mi = (int(hm.group(1)), int(hm.group(2))) if hm else (0, 0)
        return day.replace(hour=h, minute=mi, second=0, microsecond=0).isoformat(timespec="seconds")
    # A bare "1.2" / "1.2万" is a counter, not January 2nd: '.' only counts after a year.
    m = (re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})", text) or
         re.search(r"()(?<![\d.])(\d{1,2})\s*[-/月]\s*(\d{1,2})(?![\d.万wWkK])", text))
    if m:
        year = int(m.group(1)) if m.group(1) else now.year
        try:
            when = now.replace(year=year, month=int(m.group(2)), day=int(m.group(3)),
                               hour=int(hm.group(1)) if hm else 0, minute=int(hm.group(2)) if hm else 0,
                               second=0, microsecond=0)
        except ValueError:
            return ""
        if not m.group(1) and when > now + timedelta(days=1):   # "12-30" seen in January = last year
            when = when.replace(year=year - 1)
        return when.isoformat(timespec="seconds")
    return ""
