"""
多媒体热点聚焦模块：自动检测多家媒体同时报道的具体新闻事件。

流程：
1. 将所有文章标题一次性发给 LLM，由 LLM 识别出"同一具体事件"的文章组
2. 构建 TopicCluster 对象
3. 使用深度思考模型（LLM_MODEL）对每个热点簇进行综合分析
4. 回退方案：若 LLM 聚类失败，仅使用《》书名号内的游戏名做规则聚类
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass, field
from collections import defaultdict

from openai import OpenAI

import config

logger = logging.getLogger(__name__)


# ── 数据结构 ────────────────────────────────────────────────

@dataclass
class TopicCluster:
    keyword: str                        # 事件简短名称
    articles: list = field(default_factory=list)   # 相关 NewsItem 列表
    sources: set = field(default_factory=set)      # 来源媒体集合
    deep_html: str = ""                 # LLM 深度分析渲染后的 HTML


# ── LLM 语义聚类（主方案）───────────────────────────────────

_CLUSTER_PROMPT_TPL = """\
以下是今日 {total} 篇游戏新闻的标题列表（格式：编号. [媒体名] 标题）：

{numbered}

请找出其中由**多家不同媒体**同时报道的**具体新闻事件**。

判断标准（必须同时满足）：
- 是同一件具体的事情（如同一款游戏发布、同一公司裁员、同一事件声明）
- 不能仅仅是同一公司 / 同一平台 / 同一游戏的不同消息（如"Steam今日上线了XX游戏"与"Steam又推出YY游戏"不是同一事件）
- 至少来自 2 家不同媒体

请以 JSON 数组格式输出，每个元素代表一个热点事件：
[
  {{
    "event": "事件简短名称（10字以内，用于显示标题）",
    "indices": [1, 5, 8],
    "summary": "一句话描述这是什么事"
  }}
]

只输出 JSON 数组，不要任何其他文字。若没有符合条件的事件，输出空数组 []。\
"""


def _llm_cluster_articles(news_items: list, min_sources: int) -> list[TopicCluster]:
    """调用 LLM，将文章标题列表一次性发送，由 LLM 识别同一具体事件的文章组。"""
    numbered = "\n".join(
        f"{i + 1}. [{item.source}] {item.text.split(chr(10))[0][:100]}"
        for i, item in enumerate(news_items)
    )
    prompt = _CLUSTER_PROMPT_TPL.format(total=len(news_items), numbered=numbered)

    client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
    resp = client.chat.completions.create(
        model=config.LLM_FAST_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=3000,
    )
    raw = (resp.choices[0].message.content or "").strip()
    logger.debug("LLM 聚类原始响应：\n%s", raw)

    # 去掉 markdown 代码块包裹
    raw = re.sub(r"^```[^\n]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw.strip())
    raw = raw.strip()

    # 提取 JSON 数组区段
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        raise ValueError(f"LLM 未返回有效 JSON 数组，原始：{raw[:200]}")
    json_str = m.group()

    # 策略1：直接解析
    try:
        clusters_raw: list[dict] = json.loads(json_str)
    except json.JSONDecodeError:
        # 策略2：将 JSON 字符串值内的真实换行换成空格后再解析
        def _fix_newlines(text: str) -> str:
            result, in_str, prev = [], False, ""
            for ch in text:
                if ch == '"' and prev != "\\":
                    in_str = not in_str
                result.append(" " if (in_str and ch in "\n\r") else ch)
                prev = ch
            return "".join(result)

        try:
            clusters_raw = json.loads(_fix_newlines(json_str))
        except json.JSONDecodeError:
            # 策略3：用 regex 逐个提取 {…} 对象
            obj_texts = re.findall(r'\{[^{}]+\}', json_str, re.DOTALL)
            clusters_raw = []
            for ot in obj_texts:
                try:
                    clusters_raw.append(json.loads(_fix_newlines(ot)))
                except json.JSONDecodeError:
                    continue
            if not clusters_raw:
                raise ValueError(f"无法解析 LLM JSON，原始：{json_str[:300]}")

    result: list[TopicCluster] = []
    for c in clusters_raw:
        indices = [int(idx) - 1 for idx in c.get("indices", [])
                   if 1 <= int(idx) <= len(news_items)]
        if len(indices) < 2:
            continue
        articles = [news_items[i] for i in indices]
        sources = {a.source for a in articles}
        if len(sources) < min_sources:
            continue
        result.append(TopicCluster(
            keyword=c.get("event", "未知事件")[:20],
            articles=articles,
            sources=sources,
        ))

    return result


# ── 规则聚类（回退方案，仅用《》游戏名）──────────────────────

def _rule_cluster_articles(
    news_items: list,
    min_sources: int,
    max_clusters: int,
) -> list[TopicCluster]:
    """仅用《》书名号内的游戏名做关键词匹配，避免通用公司名导致伪聚类。"""
    index: dict[str, list[tuple[int, object]]] = defaultdict(list)
    for i, item in enumerate(news_items):
        title = item.text.split("\n")[0]
        for m in re.finditer(r"《(.{1,15}?)》", title):
            index[m.group(1)].append((i, item))

    raw: dict[str, TopicCluster] = {}
    for kw, entries in index.items():
        sources = {item.source for _, item in entries}
        if len(entries) >= 2 and len(sources) >= min_sources:
            raw[kw] = TopicCluster(
                keyword=kw,
                articles=[item for _, item in entries],
                sources=sources,
            )

    clusters = sorted(raw.values(), key=lambda c: len(c.articles), reverse=True)
    return clusters[:max_clusters]


# ── 聚类主入口 ───────────────────────────────────────────────

def cluster_news(
    news_items: list,
    min_sources: int | None = None,
    max_clusters: int | None = None,
) -> list[TopicCluster]:
    """
    对新闻列表进行多媒体热点聚类，优先使用 LLM 语义聚类。

    参数：
        news_items   : NewsItem 列表
        min_sources  : 最少来源媒体数（默认读 config）
        max_clusters : 最多返回热点数（默认读 config）

    返回：按文章数降序排列的 TopicCluster 列表（deep_html 尚未填充）
    """
    min_src = min_sources if min_sources is not None else config.CLUSTER_MIN_SOURCES
    max_cls = max_clusters if max_clusters is not None else config.CLUSTER_MAX_COUNT

    # 优先 LLM 聚类
    try:
        logger.info("开始 LLM 语义聚类（共 %d 篇文章）…", len(news_items))
        clusters = _llm_cluster_articles(news_items, min_src)
        # 按文章数降序，取前 max_cls 个
        clusters.sort(key=lambda c: len(c.articles), reverse=True)
        clusters = clusters[:max_cls]
        logger.info("LLM 聚类完成：检测到 %d 个多媒体热点事件", len(clusters))
        for c in clusters:
            logger.info("  「%s」: %d篇，%d家媒体（%s）",
                        c.keyword, len(c.articles), len(c.sources),
                        "、".join(sorted(c.sources)))
        return clusters
    except Exception as e:
        logger.warning("LLM 聚类失败（%s），回退到规则聚类（仅《》游戏名）", e)

    # 回退：规则聚类
    clusters = _rule_cluster_articles(news_items, min_src, max_cls)
    logger.info("规则聚类完成：检测到 %d 个热点", len(clusters))
    for c in clusters:
        logger.info("  《%s》: %d篇，%d家媒体（%s）",
                    c.keyword, len(c.articles), len(c.sources),
                    "、".join(sorted(c.sources)))
    return clusters


# ── LLM 深度分析 ─────────────────────────────────────────────

_CLUSTER_SYSTEM = """\
你是一位资深游戏媒体主编，擅长从多家媒体的多角度报道中提炼出最值得深度创作的选题方向。
"""


def analyze_clusters(clusters: list[TopicCluster]) -> None:
    """对每个热点簇调用深度思考模型进行综合分析，in-place 填充 deep_html。"""
    if not clusters:
        return
    logger.info("开始对 %d 个热点簇进行深度分析（模型：%s）…",
                len(clusters), config.LLM_MODEL)
    for c in clusters:
        try:
            c.deep_html = _analyze_one_cluster(c)
            logger.info("  热点「%s」深度分析完成", c.keyword)
        except Exception as e:
            c.deep_html = (
                f'<div style="color:#888;font-size:12px;padding:6px 0;">'
                f'深度分析失败：{html_lib.escape(str(e)[:100])}</div>'
            )
            logger.warning("  热点「%s」深度分析失败：%s", c.keyword, e)


def _analyze_one_cluster(cluster: TopicCluster) -> str:
    """调用 LLM 分析单个热点簇，返回渲染好的 HTML 卡片字符串。"""
    article_lines = "\n".join(
        f"[{art.source}] {art.text.split(chr(10))[0][:120]}"
        for art in cluster.articles
    )
    prompt = (
        f"热点事件：{cluster.keyword}\n"
        f"以下是来自 {len(cluster.sources)} 家媒体的 {len(cluster.articles)} 篇报道标题：\n"
        f"{article_lines}\n\n"
        "请综合分析这一热点，严格输出以下5行，不要其他文字：\n"
        "核心事件：一句话概括这件事\n"
        "热度原因：2-3句话说明为何多家媒体同时关注\n"
        "推荐角度：具体角度一|具体角度二|具体角度三\n"
        "注意事项：写作时需要注意的风险或敏感点\n"
        "综合评分：1-10"
    )

    client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
    resp = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": _CLUSTER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    raw = resp.choices[0].message.content or ""
    logger.debug("热点「%s」LLM 原始响应：\n%s", cluster.keyword, raw)
    return _render_cluster_html(cluster, raw)


def _render_cluster_html(cluster: TopicCluster, llm_text: str) -> str:
    """将 LLM 5行固定格式响应解析为 HTML 卡片。"""
    fields: dict[str, str] = {}
    for line in llm_text.splitlines():
        line = line.strip()
        for label in ("核心事件", "热度原因", "推荐角度", "注意事项", "综合评分"):
            for sep in ("：", ":"):
                if line.startswith(label + sep):
                    fields[label] = line[len(label) + 1:].strip()
                    break

    event      = fields.get("核心事件", "")
    reason     = fields.get("热度原因", "")
    angles_raw = fields.get("推荐角度", "")
    caution    = fields.get("注意事项", "")
    score_raw  = fields.get("综合评分", "0")

    score_m = re.search(r"\d+", score_raw)
    score   = int(score_m.group()) if score_m else 0
    score_color = "#d93025" if score >= 9 else "#f29900" if score >= 7 else "#1a73e8"

    angles: list[str] = []
    for sep in ("|", "｜", "/"):
        if sep in angles_raw:
            angles = [a.strip() for a in angles_raw.split(sep) if a.strip()]
            break
    if not angles and angles_raw:
        angles = [angles_raw]

    angles_html = "".join(
        f'<li style="margin-bottom:4px;">{html_lib.escape(a)}</li>'
        for a in angles
    )

    source_badges = " ".join(
        f'<span style="display:inline-block;background:#e8f0fe;color:#1a73e8;'
        f'font-size:11px;padding:1px 8px;border-radius:10px;margin:2px;">'
        f'{html_lib.escape(s)}</span>'
        for s in sorted(cluster.sources)
    )

    caution_html = (
        f'<p style="margin:6px 0 0;font-size:12px;color:#f29900;">'
        f'⚠️ <strong>注意：</strong>{html_lib.escape(caution)}</p>'
        if caution else ""
    )

    return (
        '<div style="background:#f8f9fa;border-left:4px solid #d93025;'
        'padding:14px 16px;border-radius:4px;">'

        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap;">'
        f'<span style="font-size:15px;font-weight:700;">{html_lib.escape(cluster.keyword)}</span>'
        f'<span style="background:{score_color};color:#fff;padding:2px 9px;'
        f'border-radius:10px;font-size:12px;">{score}/10</span>'
        f'<span style="color:#888;font-size:12px;">{len(cluster.articles)}篇报道</span>'
        f'</div>'

        f'<div style="margin-bottom:8px;">{source_badges}</div>'

        + (f'<p style="margin:0 0 6px;font-size:13px;line-height:1.6;">'
           f'<strong>核心事件：</strong>{html_lib.escape(event)}</p>'
           if event else "")

        + (f'<p style="margin:0 0 6px;font-size:13px;line-height:1.6;">'
           f'<strong>热度原因：</strong>{html_lib.escape(reason)}</p>'
           if reason else "")

        + (f'<p style="margin:0 0 4px;font-size:13px;"><strong>推荐角度：</strong></p>'
           f'<ul style="margin:0 0 6px 18px;font-size:13px;line-height:1.7;">'
           f'{angles_html}</ul>'
           if angles_html else "")

        + caution_html

        + '</div>'
    )
