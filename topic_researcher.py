from __future__ import annotations

import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request

import config


def _llm_chat(
    system_prompt: str,
    user_prompt: str,
    timeout: int = 60,
    max_tokens: int = 1800,
    model: str | None = None,
) -> dict:
    req_model = model or config.LLM_MODEL or config.LLM_FAST_MODEL
    req_data = json.dumps(
        {
            "model": req_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
        data=req_data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {
        "content": message.get("content", "") or "",
        "finish_reason": choice.get("finish_reason", "") or "",
        "usage": result.get("usage") or {},
        "model": req_model,
        "max_tokens": max_tokens,
    }


def _fetch_reader_text(url: str, timeout: int = 25) -> tuple[str, str]:
    if not url:
        return "", "缺少链接"
    try:
        req = urllib.request.Request(
            f"https://r.jina.ai/{url}",
            headers={
                "User-Agent": "Mozilla/5.0 ai-news-bot/1.0",
                "Accept": "text/markdown,text/plain",
                "X-Timeout": str(timeout),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return raw, ""
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _clean_excerpt(text: str, limit: int = 420) -> str:
    text = _normalize_whitespace(text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = text.strip()
    return text[:limit] + "..." if len(text) > limit else text


def _clean_url(url: str) -> str:
    return url.rstrip('.,);]"\'')


def _extract_reference_urls(text: str, base_link: str) -> list[str]:
    seen: set[str] = set()
    base_host = urllib.parse.urlparse(base_link).netloc.lower()
    found: list[str] = []
    for raw in re.findall(r"https?://[^\s<>\]\)\"']+", text or ""):
        url = _clean_url(raw)
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or "r.jina.ai" in host:
            continue
        if url == base_link:
            continue
        if url in seen:
            continue
        if host == base_host and len(found) >= 2:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= 4:
            break
    return found


def _detect_source_type(url: str, original_host: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    official_markers = (
        "steam",
        "playstation",
        "xbox",
        "nintendo",
        "riotgames",
        "mihoyo",
        "hoyoverse",
        "bilibili",
        "taptap",
    )
    if host == original_host:
        return "original_article"
    if any(x in host for x in official_markers):
        return "official_reference"
    return "media_reference"


def _fallback_source_bundle(title: str, link: str, source: str, article_text: str, refs: list[dict]) -> list[dict]:
    original_host = urllib.parse.urlparse(link).netloc.lower()
    bundle = [
        {
            "title": title or "原始新闻",
            "url": link,
            "source_type": "original_article",
            "credibility": "medium",
            "summary": _clean_excerpt(article_text) or f"来源：{source or original_host or '原始新闻'}",
        }
    ]
    for ref in refs:
        bundle.append(
            {
                "title": ref.get("title", "") or urllib.parse.urlparse(ref.get("url", "")).netloc,
                "url": ref.get("url", ""),
                "source_type": _detect_source_type(ref.get("url", ""), original_host),
                "credibility": "medium" if ref.get("summary") else "low",
                "summary": ref.get("summary", "") or "待进一步核查的参考来源。",
            }
        )
    return bundle


def _extract_json_object(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return None


def _normalize_string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalize_source_bundle(value, fallback_sources: list[dict]) -> list[dict]:
    if not isinstance(value, list):
        return fallback_sources
    items: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        url = str(raw.get("url", "")).strip()
        source_type = str(raw.get("source_type", "")).strip() or "reference"
        credibility = str(raw.get("credibility", "")).strip() or "medium"
        summary = str(raw.get("summary", "")).strip()
        if not title and not url and not summary:
            continue
        items.append(
            {
                "title": title or url or "参考来源",
                "url": url,
                "source_type": source_type,
                "credibility": credibility,
                "summary": summary,
            }
        )
    return items or fallback_sources


def _normalize_claims(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    claims: list[dict] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        claim_text = str(raw.get("claim_text", "")).strip()
        if not claim_text:
            continue
        claims.append(
            {
                "claim_text": claim_text,
                "status": str(raw.get("status", "")).strip() or "unverified",
                "evidence_sources": _normalize_string_list(raw.get("evidence_sources")),
                "risk_level": str(raw.get("risk_level", "")).strip() or "medium",
                "editor_comment": str(raw.get("editor_comment", "")).strip(),
            }
        )
    return claims


def _normalize_report(report: dict | None, fallback_sources: list[dict], title: str) -> dict:
    if not isinstance(report, dict):
        report = {}
    topic_summary = str(report.get("topic_summary", "")).strip() or f"围绕《{title}》的选题研究资料。"
    source_bundle = _normalize_source_bundle(report.get("source_bundle"), fallback_sources)
    claims = _normalize_claims(report.get("claims"))
    verified_facts = _normalize_string_list(report.get("verified_facts"))
    unverified_points = _normalize_string_list(report.get("unverified_points"))
    editor_notes = _normalize_string_list(report.get("editor_notes"))
    draft_outline = _normalize_string_list(report.get("draft_outline"))
    draft_preview = str(report.get("draft_preview", "")).strip()
    return {
        "topic_summary": topic_summary,
        "source_bundle": source_bundle,
        "claims": claims,
        "verified_facts": verified_facts,
        "unverified_points": unverified_points,
        "editor_notes": editor_notes,
        "draft_outline": draft_outline,
        "draft_preview": draft_preview,
    }


def _render_list(title: str, items: list[str], bullet_color: str = "#1a73e8") -> str:
    if not items:
        return ""
    lis = "".join(
        f"<li style='margin:4px 0;'><span style='color:{bullet_color};'>•</span> {html.escape(x)}</li>"
        for x in items
    )
    return (
        f"<div style='margin-top:10px;'>"
        f"<div style='font-size:12px;color:#555;font-weight:600;margin-bottom:4px;'>{html.escape(title)}</div>"
        f"<ul style='margin:0 0 0 16px;padding:0;font-size:13px;line-height:1.7;'>{lis}</ul>"
        f"</div>"
    )


def _render_sources(sources: list[dict]) -> str:
    if not sources:
        return ""
    rows = []
    for item in sources:
        title = html.escape(item.get("title", "参考来源"))
        url = item.get("url", "")
        source_type = html.escape(item.get("source_type", "reference"))
        credibility = html.escape(item.get("credibility", "medium"))
        summary = html.escape(item.get("summary", ""))
        title_html = f"<a href='{html.escape(url)}' target='_blank' style='color:#1a73e8;text-decoration:none;'>{title}</a>" if url else title
        rows.append(
            "<div style='padding:8px 0;border-top:1px solid #eef2f7;'>"
            f"<div style='font-size:13px;font-weight:600;'>{title_html}</div>"
            f"<div style='font-size:11px;color:#888;margin:2px 0 4px;'>类型：{source_type} | 可信度：{credibility}</div>"
            f"<div style='font-size:12px;line-height:1.6;color:#444;'>{summary}</div>"
            "</div>"
        )
    return (
        "<div style='margin-top:10px;'>"
        "<div style='font-size:12px;color:#555;font-weight:600;margin-bottom:4px;'>资料来源</div>"
        + "".join(rows)
        + "</div>"
    )


def _render_claims(claims: list[dict]) -> str:
    if not claims:
        return ""
    rows = []
    status_label = {
        "confirmed": ("已确认", "#2e7d32"),
        "disputed": ("存争议", "#d93025"),
        "unverified": ("待核实", "#f29900"),
    }
    for claim in claims[:8]:
        label, color = status_label.get(claim.get("status", ""), ("待核实", "#f29900"))
        evidence = "；".join(claim.get("evidence_sources", []))
        comment = claim.get("editor_comment", "")
        rows.append(
            "<div style='padding:8px 0;border-top:1px solid #eef2f7;'>"
            f"<div style='font-size:13px;font-weight:600;'>{html.escape(claim.get('claim_text', ''))}</div>"
            f"<div style='font-size:11px;color:{color};margin:3px 0;'>状态：{label} | 风险：{html.escape(claim.get('risk_level', 'medium'))}</div>"
            + (f"<div style='font-size:12px;line-height:1.6;color:#444;'>证据：{html.escape(evidence)}</div>" if evidence else "")
            + (f"<div style='font-size:12px;line-height:1.6;color:#666;'>编辑备注：{html.escape(comment)}</div>" if comment else "")
            + "</div>"
        )
    return (
        "<div style='margin-top:10px;'>"
        "<div style='font-size:12px;color:#555;font-weight:600;margin-bottom:4px;'>事实核对卡</div>"
        + "".join(rows)
        + "</div>"
    )


def _render_paragraph(title: str, text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    paragraphs = "".join(
        f"<p style='margin:0 0 6px;font-size:13px;line-height:1.75;color:#333;'>{html.escape(part.strip())}</p>"
        for part in re.split(r"\n{2,}", text)
        if part.strip()
    )
    return (
        f"<div style='margin-top:10px;'>"
        f"<div style='font-size:12px;color:#555;font-weight:600;margin-bottom:4px;'>{html.escape(title)}</div>"
        f"{paragraphs}"
        f"</div>"
    )


def render_research_html(item: dict) -> str:
    notice = str(item.get("notice", "")).strip()
    notice_html = (
        f"<div style='color:#f29900;font-size:12px;line-height:1.6;margin-bottom:8px;'>{html.escape(notice)}</div>"
        if notice
        else ""
    )
    updated_at = html.escape(str(item.get("updated_at", "")))
    return (
        "<div style='background:#fff;border-left:4px solid #6d4aff;padding:12px 14px;border-radius:4px;margin-top:4px;'>"
        "<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;'>"
        "<strong style='font-size:14px;color:#3b2a7a;'>资料研究</strong>"
        f"<span style='background:#ede7ff;color:#5b3fd4;padding:2px 8px;border-radius:10px;font-size:11px;'>更新于 {updated_at[:16].replace('T', ' ')}</span>"
        "</div>"
        + notice_html
        + _render_paragraph("选题摘要", str(item.get("topic_summary", "")))
        + _render_sources(item.get("source_bundle", []))
        + _render_claims(item.get("claims", []))
        + _render_list("已确认事实", item.get("verified_facts", []), "#2e7d32")
        + _render_list("待核实点", item.get("unverified_points", []), "#f29900")
        + _render_list("编辑提示", item.get("editor_notes", []), "#5f6368")
        + _render_list("初稿提纲", item.get("draft_outline", []), "#5b3fd4")
        + _render_paragraph("初稿预览", str(item.get("draft_preview", "")))
        + "</div>"
    )


def _build_fallback_item(title: str, link: str, source: str, source_bundle: list[dict], notice: str) -> dict:
    return {
        "title": title,
        "link": link,
        "source": source,
        "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "topic_summary": f"《{title}》已有基础资料，但结构化研究结果暂未完整生成。",
        "source_bundle": source_bundle,
        "claims": [],
        "verified_facts": [],
        "unverified_points": ["建议补充官方公告、版本更新记录或平台页面进行二次核实。"],
        "editor_notes": ["本次结果为兜底研究包，适合继续手动补充资料后再生成初稿。"],
        "draft_outline": ["事件概述", "事实核对", "行业影响", "写作角度"],
        "draft_preview": "",
        "notice": notice,
    }


def generate_research_package(title: str, link: str, source: str = "") -> dict:
    article_text, article_error = _fetch_reader_text(link)
    article_excerpt = _clean_excerpt(article_text, 900)
    ref_urls = _extract_reference_urls(article_text, link)
    ref_contexts: list[dict] = []
    for ref_url in ref_urls[:2]:
        ref_text, ref_error = _fetch_reader_text(ref_url, timeout=18)
        ref_contexts.append(
            {
                "title": urllib.parse.urlparse(ref_url).netloc or ref_url,
                "url": ref_url,
                "summary": _clean_excerpt(ref_text, 240) if ref_text else f"抓取失败：{ref_error}",
            }
        )

    fallback_sources = _fallback_source_bundle(title, link, source, article_excerpt or article_text, ref_contexts)
    notice = ""
    if article_error:
        notice = f"原文抓取失败：{article_error}。研究结果主要基于标题和已有链接。"

    system_prompt = (
        "你是一名游戏媒体研究员兼事实核查助手。"
        "你的任务是基于已有材料输出结构化研究结果，严格区分已确认事实与待核实信息。"
        "请只输出合法 JSON 对象，不要使用 Markdown。"
    )
    user_prompt = (
        f"请围绕这个游戏选题生成研究包。\n\n"
        f"标题：{title}\n"
        f"原始链接：{link}\n"
        f"来源站点：{source}\n\n"
        f"原始新闻摘要：\n{article_excerpt or '（原文抓取不足，仅提供标题）'}\n\n"
        f"补充参考资料：\n{json.dumps(ref_contexts, ensure_ascii=False, indent=2)}\n\n"
        "请只输出 JSON，对象必须包含这些字段：\n"
        "{\n"
        '  "topic_summary": "1-3 句话概括这个选题的核心价值",\n'
        '  "source_bundle": [\n'
        '    {"title": "", "url": "", "source_type": "original_article|official_reference|media_reference|community_reference", "credibility": "high|medium|low", "summary": ""}\n'
        "  ],\n"
        '  "claims": [\n'
        '    {"claim_text": "", "status": "confirmed|unverified|disputed", "evidence_sources": ["来源标题"], "risk_level": "low|medium|high", "editor_comment": ""}\n'
        "  ],\n"
        '  "verified_facts": ["..."],\n'
        '  "unverified_points": ["..."],\n'
        '  "editor_notes": ["..."],\n'
        '  "draft_outline": ["..."],\n'
        '  "draft_preview": "基于已确认事实写一段 150-250 字的资讯稿草案，如证据不足可写空字符串"\n'
        "}\n\n"
        "要求：\n"
        "- 不要编造不存在的官方来源\n"
        "- 如果资料不足，明确写入 unverified_points\n"
        "- claims 至少输出 3 条\n"
        "- draft_outline 输出 4-6 条\n"
        "- source_bundle 中优先保留原始新闻和高可信来源\n"
    )

    llm_result = _llm_chat(system_prompt, user_prompt, timeout=65, max_tokens=2200)
    report = _extract_json_object(str(llm_result.get("content", "") or ""))
    if not report:
        repair_prompt = (
            "你上一次输出的不是合法 JSON。请严格修正为合法 JSON，字段保持不变，不要输出任何额外文字。\n\n"
            f"原始输出：\n{str(llm_result.get('content', '') or '')[:4000]}"
        )
        llm_result = _llm_chat(system_prompt, repair_prompt, timeout=45, max_tokens=2200, model=config.LLM_MODEL)
        report = _extract_json_object(str(llm_result.get("content", "") or ""))

    if not report:
        item = _build_fallback_item(title, link, source, fallback_sources, notice or "结构化研究结果生成失败。")
        item["html"] = render_research_html(item)
        return item

    item = _normalize_report(report, fallback_sources, title)
    item.update(
        {
            "title": title,
            "link": link,
            "source": source,
            "updated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notice": notice,
        }
    )
    item["html"] = render_research_html(item)
    try:
        print(
            "[research]",
            json.dumps(
                {
                    "title": title[:120],
                    "model": llm_result.get("model", ""),
                    "max_tokens": llm_result.get("max_tokens", 0),
                    "finish_reason": llm_result.get("finish_reason", ""),
                    "source_count": len(item.get("source_bundle", [])),
                    "claim_count": len(item.get("claims", [])),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception:
        pass
    return item
