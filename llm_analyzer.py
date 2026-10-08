"""
调用 LLM 对游戏新闻进行选题价值分析，返回结构化的 Top N 选题列表。
也支持对单篇文章进行批量并发分析。
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai import OpenAI

import config

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = config.EDITOR_PERSONA + "\n\n" + config.SCORING_RUBRIC + "\n\n" + config.SCORING_METHOD

USER_PROMPT_TEMPLATE = """\
以下是今天从游戏新闻频道采集到的 {count} 条新闻：

{news_text}

请从中挑选出最值得写稿的 Top {top_n} 条选题，并以 JSON 数组格式输出，格式如下：

```json
[
  {{
    "rank": 1,
    "title": "建议的文章标题",
    "score": 8,
    "热度": 2, "深度": 2, "时效": 1, "共鸣": 2, "适配": 1,
    "reason": "选题理由（2-3句话，逐条引用命中的信号）",
    "angles": ["写作角度1", "写作角度2", "写作角度3"],
    "source_index": 3
  }},
  ...
]
```

其中：
- rank：排名（1最高）
- title：你建议的文章标题（吸引眼球、适合游戏媒体）
- score：选题价值总分，1-10分（= 热度+深度+时效+共鸣+适配 五维之和，再经加减分修正）
- 热度/深度/时效/共鸣/适配：五个维度的单独得分（分别为 0-2/0-2/0-1/0-2/0-3 分），必须与 score 的构成一致
- reason：为什么这条值得写（必须逐条引用命中的信号，如"涉及R星+多家媒体同时报道→热度2分"）
- angles：至少2个具体的写作切入角度
- source_index：对应原始新闻列表中的编号（[1]、[2]...中的数字）

只输出 JSON，不要有任何额外说明文字。
"""


@dataclass
class TopicResult:
    rank: int
    title: str
    score: int
    reason: str
    angles: list[str] = field(default_factory=list)
    source_index: int = 0
    source_text: str = ""
    source_link: str = ""
    score_breakdown: dict = field(default_factory=dict)


def analyze(news_text: str, top_n: int = config.TOP_N) -> list[TopicResult]:
    """
    将新闻文本发给 LLM 分析，返回 TopicResult 列表。
    news_text 应为 slack_reader.format_for_llm() 的输出。
    """
    if not news_text.strip():
        logger.warning("没有收到任何新闻内容，跳过 LLM 分析")
        return []

    news_count = _count_news_items(news_text)
    user_prompt = USER_PROMPT_TEMPLATE.format(
        count=news_count,
        news_text=news_text,
        top_n=top_n,
    )

    client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)

    logger.info("正在调用 LLM（%s）分析 %d 条新闻…", config.LLM_MODEL, news_count)

    response = client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content or ""
    logger.debug("LLM 原始响应：\n%s", raw)

    results = _parse_response(raw)
    logger.info("LLM 返回 %d 条选题建议", len(results))
    return results


def _parse_response(raw: str) -> list[TopicResult]:
    """从 LLM 响应中提取 JSON 数组，容忍 markdown 代码块包裹。"""
    # 去掉可能存在的 ```json ... ``` 包裹
    json_text = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

    # 尝试找到 [...] 数组部分
    match = re.search(r"\[.*\]", json_text, re.DOTALL)
    if match:
        json_text = match.group(0)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        logger.error("LLM 响应解析失败：%s\n原始内容：%s", e, raw[:500])
        return []

    results: list[TopicResult] = []
    for item in data:
        try:
            breakdown: dict = {}
            if isinstance(item.get("score_breakdown"), dict):
                breakdown.update(item["score_breakdown"])
            for dim in ("热度", "深度", "时效", "共鸣", "适配"):
                if dim in item:
                    try:
                        breakdown[dim] = int(item[dim])
                    except (TypeError, ValueError):
                        pass
            results.append(
                TopicResult(
                    rank=int(item.get("rank", 0)),
                    title=str(item.get("title", "")),
                    score=int(item.get("score", 0)),
                    reason=str(item.get("reason", "")),
                    angles=list(item.get("angles", [])),
                    source_index=int(item.get("source_index", 0)),
                    score_breakdown=breakdown,
                )
            )
        except (TypeError, ValueError) as e:
            logger.warning("跳过一条格式异常的选题：%s", e)

    results.sort(key=lambda r: r.rank)
    return results


def _count_news_items(news_text: str) -> int:
    """统计新闻文本中的条目数量（以 [数字] 开头的行）。"""
    return len(re.findall(r"^\[\d+\]", news_text, re.MULTILINE))


# ── 单篇文章分析（用于批量预计算）────────────────────────────────


@dataclass
class ArticleAnalysis:
    judgment: str = ""
    score: int = 0
    reason: str = ""
    angles: list[str] = field(default_factory=list)
    error: str = ""


def analyze_single(title: str) -> ArticleAnalysis:
    """对单篇文章标题进行选题分析，返回 ArticleAnalysis。"""
    result, _ = _analyze_single_with_usage(title)
    return result


def _parse_single_response(text: str) -> ArticleAnalysis:
    """解析 LLM 返回的4行固定格式文本。"""
    result = ArticleAnalysis()
    for line in text.splitlines():
        line = line.strip()
        for prefix, key in [
            ("判断：", "j"), ("判断:", "j"),
            ("评分：", "s"), ("评分:", "s"),
            ("理由：", "r"), ("理由:", "r"),
            ("角度：", "a"), ("角度:", "a"),
        ]:
            if line.startswith(prefix):
                val = line[len(prefix):].strip()
                if key == "j":
                    result.judgment = val
                elif key == "s":
                    # 用 re 取第一个整数，避免 "9/10" 被解析成 "91"
                    m = re.search(r"\d+", val)
                    result.score = int(m.group()) if m else 0
                elif key == "r":
                    result.reason = val
                elif key == "a":
                    for sep in ("|", "｜", "/"):
                        if sep in val:
                            result.angles = [v.strip() for v in val.split(sep)]
                            break
                    else:
                        result.angles = [val]
                break
    return result


def analyze_articles_batch(
    news_items: list,
    max_workers: int = 10,
    indices: "set[int] | None" = None,
) -> "dict[int, ArticleAnalysis]":
    """
    并发分析指定文章，返回 {文章索引: ArticleAnalysis} 字典。
    indices 为 None 时分析全部；否则只分析指定下标（节省 token）。
    """
    if indices is None:
        indices = set(range(len(news_items)))

    subset = [(i, news_items[i]) for i in sorted(indices) if i < len(news_items)]
    total = len(subset)
    logger.info("开始并发分析 %d 篇文章（%d 线程，模型：%s）…",
                total, min(max_workers, total or 1), config.LLM_FAST_MODEL)

    results: dict[int, ArticleAnalysis] = {}
    futures: dict = {}
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_text_tokens = 0
    total_reasoning_tokens = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, item in subset:
            title = item.text.split("\n")[0][:150]
            futures[executor.submit(_analyze_single_with_usage, title)] = i

        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            try:
                analysis, usage = future.result()
                results[idx] = analysis
                if usage:
                    total_prompt_tokens += usage.get("prompt", 0)
                    total_completion_tokens += usage.get("completion", 0)
                    total_text_tokens += usage.get("text", 0)
                    total_reasoning_tokens += usage.get("reasoning", 0)
            except Exception as e:
                results[idx] = ArticleAnalysis(error=str(e))
            done += 1
            if done % 10 == 0 or done == total:
                logger.info("  文章分析进度：%d / %d", done, total)

    ok = sum(1 for a in results.values() if not a.error)
    logger.info("批量分析完成：%d 成功 / %d 失败", ok, total - ok)
    logger.info(
        "Token 消耗统计：prompt=%d  completion=%d（text=%d, reasoning=%d）  合计=%d",
        total_prompt_tokens,
        total_completion_tokens,
        total_text_tokens,
        total_reasoning_tokens,
        total_prompt_tokens + total_completion_tokens,
    )
    return results


def _analyze_single_with_usage(title: str) -> "tuple[ArticleAnalysis, dict]":
    """调用 LLM 分析单篇文章，同时返回 token 用量。"""
    prompt = (
        f"游戏新闻标题：{title}\n\n"
        "请对这条新闻进行选题价值评估，严格输出恰好4行，不要其他文字：\n"
        "判断：适合/可参考/不适合\n"
        "评分：1-10\n"
        "理由：2-3句话说明选题价值（结合上述判断维度）\n"
        "角度：具体写作切入角度一|具体写作切入角度二"
    )
    # 批量文章筛选用轻量 fast 模型，减少不必要的深度推理 token 消耗
    client = OpenAI(api_key=config.LLM_API_KEY, base_url=config.LLM_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=config.LLM_FAST_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1500,
        )
        text = resp.choices[0].message.content or ""
        usage = {}
        if resp.usage:
            details = resp.usage.completion_tokens_details
            usage = {
                "prompt": resp.usage.prompt_tokens,
                "completion": resp.usage.completion_tokens,
                "text": getattr(details, "text_tokens", 0) or 0,
                "reasoning": getattr(details, "reasoning_tokens", 0) or 0,
            }
        return _parse_single_response(text), usage
    except Exception as e:
        return ArticleAnalysis(error=str(e)), {}
