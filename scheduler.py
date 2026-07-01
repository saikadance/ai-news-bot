"""
主入口：整合 news_fetcher → llm_analyzer → feishu_sender，支持：
  - 立即运行一次（python scheduler.py --now）
  - 每日定时运行（python scheduler.py，默认按 .env 中的 SCHEDULE_TIME）
"""
from __future__ import annotations

import argparse
import base64
import html as html_lib
import json
import logging
import mimetypes
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import schedule
import time

import config
import news_fetcher
import llm_analyzer
import feishu_sender
import slack_sender
import url_cache
import gist_uploader
import analysis_cache
import share_url_helper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


_TOP5_CACHE_FILE = Path(__file__).parent / "top5_cache.json"
_PUSH_STATE_FILE = Path(__file__).parent / "push_state.json"
_KOL_REPORT_FILES = [
    Path(__file__).parent / "kol_signal" / "latest_report.json",
    Path(__file__).parent / "kol_signal" / "output" / "latest_report.json",
]
_LOCAL_TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", os.getenv("TZ", "Asia/Hong_Kong")))


def _save_top5_cache(results: list) -> None:
    """将 Top5 结果序列化到本地文件，供无新文章时复用。"""
    data = [
        {
            "rank": r.rank,
            "title": r.title,
            "score": r.score,
            "reason": r.reason,
            "angles": r.angles,
            "source_link": r.source_link,
            "source_text": r.source_text,
        }
        for r in results
    ]
    with open(_TOP5_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_top5_cache() -> list:
    """加载上次保存的 Top5 缓存，失败时返回空列表。"""
    if not _TOP5_CACHE_FILE.exists():
        return []
    try:
        with open(_TOP5_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [
            llm_analyzer.TopicResult(
                rank=d["rank"],
                title=d["title"],
                score=d["score"],
                reason=d["reason"],
                angles=d.get("angles", []),
                source_link=d.get("source_link", ""),
                source_text=d.get("source_text", ""),
            )
            for d in data
        ]
    except Exception as e:
        logger.warning("加载 Top5 缓存失败：%s", e)
        return []


def _now_local() -> datetime:
    return datetime.now(_LOCAL_TZ)


def _load_push_state() -> dict:
    if not _PUSH_STATE_FILE.exists():
        return {}
    try:
        with open(_PUSH_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("加载推送状态失败：%s", e)
        return {}


def _save_push_state(data: dict) -> None:
    try:
        with open(_PUSH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("保存推送状态失败：%s", e)


def _was_pushed_today(push_state: dict, local_day: str) -> bool:
    return str(push_state.get("last_push_date", "")).strip() == local_day


def _mark_pushed_today(push_state: dict, *, local_now: datetime, news_count: int, report_url: str) -> None:
    push_state.update({
        "last_push_date": local_now.strftime("%Y-%m-%d"),
        "last_push_at": local_now.isoformat(),
        "last_news_count": int(news_count),
        "last_report_url": report_url,
    })
    _save_push_state(push_state)


def _push_reports(
    results: list,
    date_str: str,
    news_count: int,
    report_url: str,
    *,
    push_feishu: bool = True,
    push_slack: bool = True,
) -> bool:
    """按目标渠道发送报告，便于测试时单独关闭 Slack。"""
    sent_any = False
    if push_feishu:
        sent_any = feishu_sender.send_report(results, date_str, news_count, report_url) or sent_any
    else:
        logger.info("已跳过飞书推送")

    if push_slack:
        sent_any = slack_sender.send_report(results, date_str, news_count, report_url) or sent_any
    else:
        logger.info("已跳过 Slack 推送")
    return sent_any


def run_once(
    *,
    push_feishu: bool = True,
    push_slack: bool = True,
    force_push: bool = False,
    persist_url_cache: bool = True,
) -> None:
    """执行一次完整的「拉取 → 分析 → 推送」流程。"""
    local_now = _now_local()
    date_str = local_now.strftime("%Y-%m-%d %H:%M")
    local_day = local_now.strftime("%Y-%m-%d")
    push_state = _load_push_state()
    logger.info("=" * 60)
    if not force_push and _was_pushed_today(push_state, local_day):
        logger.info("今日 %s 已完成推送，跳过重复运行", local_day)
        return
    logger.info("开始运行选题分析 [%s]", date_str)

    # 1. 从 RSS 源抓取全量新闻（不做 url_cache 过滤）
    try:
        rss_items = news_fetcher.fetch_news(config.LOOKBACK_HOURS)
    except Exception as e:
        logger.error("RSS 新闻抓取失败：%s", e)
        return

    # 2. url_cache 过滤 → 今日新增文章（仅用于飞书 Top5 推送）
    ucache = url_cache.load()
    new_items = url_cache.filter_new(rss_items, ucache)

    # 3. 从分析缓存恢复历史文章列表，与 RSS 合并成完整文章集
    acache = analysis_cache.load()
    cached_items = analysis_cache.get_cached_news_items(acache)
    favorite_items = analysis_cache.load_favorite_news_items()
    logger.info("??????? %d ?????", len(cached_items))
    logger.info("??????? %d ?????", len(favorite_items))

    # all_items = ???? + ?????? + ?? RSS ??
    cached_and_favorites = analysis_cache.merge_items(cached_items, favorite_items)
    all_items = analysis_cache.merge_items(cached_and_favorites, rss_items)
    logger.info("合并后共 %d 篇文章（HTML 展示 + 热点聚类用）", len(all_items))

    if not all_items:
        logger.warning("没有任何文章（RSS 和缓存均为空），跳过本次推送")
        sent_any = _push_reports([], date_str, 0, "", push_feishu=push_feishu, push_slack=push_slack)
        if sent_any:
            _mark_pushed_today(push_state, local_now=_now_local(), news_count=0, report_url="")
        return

    # 4. Top5 精选：把今日新增文章标题一次性打包发给 LLM（只有 1 次 LLM 调用）
    results: list = []
    if new_items:
        news_text = news_fetcher.format_for_llm(new_items)
        link_map = {i + 1: item.permalink for i, item in enumerate(new_items)}
        try:
            results = llm_analyzer.analyze(news_text, config.TOP_N)
        except Exception as e:
            logger.error("LLM Top5 分析失败：%s", e)
        for r in results:
            r.source_link = link_map.get(r.source_index, "")
            if 0 < r.source_index <= len(new_items):
                r.source_text = new_items[r.source_index - 1].text[:100]
        if results:
            _save_top5_cache(results)
    else:
        logger.info("今日无新增文章，从缓存恢复上次 Top5")
        results = _load_top5_cache()
        if results:
            logger.info("复用上次 Top5 缓存（%d 条）", len(results))

    # 5. 单篇预分析已永久关闭——HTML 页面改为点击按钮实时调用 Render API
    article_analyses = analysis_cache.to_index_map(all_items, acache, {})

    # 6. 热点聚类、社交热搜均已关闭

    # 7. 生成本地 HTML 汇总页（用完整 all_items）
    html_path = _generate_html(all_items, date_str, article_analyses, results=results)
    logger.info("本地新闻汇总页已生成：%s（共 %d 篇文章）", html_path, len(all_items))

    # 9. 根据 SHARE_MODE 生成可共享链接
    share_url = _get_share_url(html_path)

    # 10. 推送飞书
    if not new_items and not force_push:
        logger.info("今日无新增文章，跳过本次推送")
        return

    if new_items and persist_url_cache:
        # 先写 url_cache，防止多次触发时重复推送
        url_cache.save(new_items, ucache)
    elif new_items:
        logger.info("测试模式：跳过 url_cache 写入，不影响正式日报去重状态")

    report_url = share_url or html_path
    sent_any = _push_reports(
        results,
        date_str,
        len(new_items),
        report_url,
        push_feishu=push_feishu,
        push_slack=push_slack,
    )
    if sent_any:
        _mark_pushed_today(
            push_state,
            local_now=_now_local(),
            news_count=len(new_items),
            report_url=report_url,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="游戏新闻选题 Bot")
    parser.add_argument(
        "--now",
        action="store_true",
        help="立即运行一次，不等待定时调度",
    )
    parser.add_argument(
        "--feishu-only",
        action="store_true",
        help="只发送飞书，不发送 Slack（适合测试推送）",
    )
    parser.add_argument(
        "--force-push",
        action="store_true",
        help="即使今日没有新增新闻，也强制生成页面并推送一次",
    )
    parser.add_argument(
        "--no-url-cache-save",
        action="store_true",
        help="运行后不写入 seen_urls/url_cache，适合测试推送避免影响正式日报",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="运行后启动本地预览服务器并自动打开浏览器（配合 --now 使用）",
    )
    args = parser.parse_args()

    # 启动前校验配置
    try:
        config.validate()
    except EnvironmentError as e:
        logger.error("配置检查未通过：%s", e)
        sys.exit(1)

    if args.now:
        run_once(
            push_feishu=True,
            push_slack=not args.feishu_only,
            force_push=args.force_push,
            persist_url_cache=not args.no_url_cache_save,
        )
        if args.serve:
            import server as _srv
            import threading
            t = threading.Thread(target=_srv.start, kwargs={"open_browser": True}, daemon=True)
            t.start()
            logger.info("本地服务器已启动：http://localhost:%d  （关闭窗口即停止）", _srv.PORT)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return

    # 定时调度模式
    schedule_time = config.SCHEDULE_TIME
    logger.info("Bot 启动，将在每天 %s 自动运行（Ctrl+C 退出）", schedule_time)
    schedule.every().day.at(schedule_time).do(run_once)

    # 启动时也立即运行一次（可按需注释掉）
    logger.info("启动时先运行一次…")
    run_once()

    while True:
        schedule.run_pending()
        time.sleep(30)


def _build_analysis_card(a: "llm_analyzer.ArticleAnalysis") -> str:
    """将 ArticleAnalysis 渲染成与 Top5 风格一致的 HTML 卡片。"""
    if a.error:
        return f'<div style="color:#888;font-size:12px;padding:6px 0;">分析失败：{html_lib.escape(a.error[:80])}</div>'
    if not a.judgment and not a.reason:
        return '<div style="color:#888;font-size:12px;padding:6px 0;">（未获得分析结果）</div>'

    score_color = "#d93025" if a.score >= 9 else "#f29900" if a.score >= 7 else "#888"
    angles_html = "".join(f"<li>{html_lib.escape(ang)}</li>" for ang in a.angles if ang)
    return (
        '<div style="background:#fff;border-left:4px solid #1a73e8;'
        'padding:12px 14px;border-radius:4px;margin-top:6px;">'
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">'
        f'<strong style="font-size:14px;">{html_lib.escape(a.judgment)}</strong>'
        f'<span style="background:{score_color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:12px;">{a.score}/10</span>'
        '</div>'
        f'<p style="margin:4px 0;font-size:13px;line-height:1.6;">'
        f'<strong>选题理由：</strong>{html_lib.escape(a.reason)}</p>'
        + (f'<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.7;">{angles_html}</ul>' if angles_html else "")
        + '</div>'
    )


def _load_kol_report() -> dict:
    for path in _KOL_REPORT_FILES:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            logger.warning("加载 KOL 报告失败（%s）：%s", path, e)
    return {}


def _inline_or_remote_image_src(
    local_path: str,
    remote_url: str,
    embedded_data_url: str = "",
) -> str:
    if embedded_data_url.startswith("data:image/"):
        return embedded_data_url
    try:
        path = Path(local_path)
        if not path.is_absolute():
            path = Path(__file__).parent / path
        if path.exists() and path.is_file():
            mime, _ = mimetypes.guess_type(path.name)
            mime = mime or "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{encoded}"
    except Exception:
        pass
    return remote_url


def _build_kol_section_html(report: dict) -> str:
    items = report.get("posts_preview") or []
    notes = report.get("notes") or []
    if not items and not notes:
        return ""

    cards = []
    for idx, item in enumerate(items[:10], start=1):
        title = html_lib.escape(str(item.get("title", "")).strip() or "未命名内容")
        text = html_lib.escape(str(item.get("text", "")).strip())
        url = html_lib.escape(str(item.get("url", "")).strip(), quote=True)
        account_name = html_lib.escape(str(item.get("account_name", "")).strip() or "KOL")
        platform = html_lib.escape(str(item.get("platform", "")).strip() or "social")
        published_at = html_lib.escape(str(item.get("published_at", "")).strip())
        score = item.get("score", 0)
        media_urls = item.get("media_urls") or []
        downloaded_media_paths = item.get("downloaded_media_paths") or []
        matched_news = item.get("matched_news") or []
        source_label = f"KOL/{platform}@{account_name}"

        images_html = ""
        if media_urls:
            thumb_items = []
            for media_idx, media_url in enumerate(media_urls[:4]):
                esc_media = html_lib.escape(str(media_url), quote=True)
                local_path = str(downloaded_media_paths[media_idx]) if media_idx < len(downloaded_media_paths) else ""
                img_src = html_lib.escape(_inline_or_remote_image_src(local_path, str(media_url)), quote=True)
                thumb_items.append(
                    f'<a class="kol-thumb" href="{esc_media}" target="_blank">'
                    f'<img src="{img_src}" loading="lazy" alt="{title}"></a>'
                )
            images_html = f'<div class="kol-thumbs">{"".join(thumb_items)}</div>'

        matched_html = ""
        if matched_news:
            lines = []
            for news in matched_news[:3]:
                news_title = html_lib.escape(str(news.get("title", "")).strip())
                news_link = html_lib.escape(str(news.get("link", "")).strip(), quote=True)
                news_source = html_lib.escape(str(news.get("source", "")).strip())
                lines.append(
                    f'<li><a href="{news_link}" target="_blank">{news_title}</a>'
                    f'<span class="kol-match-src">{news_source}</span></li>'
                )
            matched_html = (
                '<div class="kol-matched"><div class="kol-subtitle">关联新闻</div>'
                f'<ul>{"".join(lines)}</ul></div>'
            )

        cards.append(
            f"""
      <div class="kol-card">
        <div class="kol-card-head">
          <div class="kol-card-meta">
            <span class="kol-rank">#{idx}</span>
            <span class="kol-account">{account_name}</span>
            <span class="kol-platform">{platform}</span>
            <span class="kol-score">传播分 {score}</span>
          </div>
          <button class="btn-star" onclick="toggleFavorite(this)"
            data-link="{url}" data-title="{title}" data-source="{html_lib.escape(source_label, quote=True)}">☆</button>
        </div>
        <a class="kol-title" href="{url}" target="_blank">{title}</a>
        <div class="kol-pub">{published_at}</div>
        {f'<p class="kol-text">{text}</p>' if text else ''}
        {images_html}
        {matched_html}
      </div>"""
        )

    notes_html = ""
    if notes:
        note_lines = "".join(f"<li>{html_lib.escape(str(x))}</li>" for x in notes[:4])
        notes_html = f'<ul class="kol-notes">{note_lines}</ul>'

    count = int(report.get("posts_count") or len(items))
    return f"""
<div id="kol-section">
  <h2>KOL 信号观察 <span class="kol-count">{count}</span></h2>
  <p class="kol-desc">观察头部账号近期公开内容，并与现有新闻缓存做轻量交叉验证。当前优先展示最近抓到的微博内容。</p>
  {notes_html}
  <div class="kol-grid">{''.join(cards) if cards else '<div class="kol-empty">当前还没有可展示的 KOL 内容。</div>'}</div>
</div>
"""


def _build_kol_section_html(report: dict) -> str:
    items = report.get("posts_preview") or []
    notes = report.get("notes") or []
    if not items and not notes:
        return ""

    platform_labels = {
        "weibo": "微博",
        "x": "X",
        "twitter": "X",
        "bilibili": "B站",
    }

    cards = []
    for idx, item in enumerate(items[:10], start=1):
        title = html_lib.escape(str(item.get("title", "")).strip() or "未命名内容", quote=True)
        text = html_lib.escape(str(item.get("text", "")).strip())
        url = html_lib.escape(str(item.get("url", "")).strip(), quote=True)
        account_name = html_lib.escape(str(item.get("account_name", "")).strip() or "KOL账号", quote=True)
        platform_raw = str(item.get("platform", "")).strip().lower() or "social"
        platform = html_lib.escape(platform_labels.get(platform_raw, platform_raw), quote=True)
        published_at = html_lib.escape(str(item.get("published_at", "")).strip())
        score = item.get("score", 0)
        media_urls = item.get("media_urls") or []
        downloaded_media_paths = item.get("downloaded_media_paths") or []
        embedded_media_data_urls = item.get("embedded_media_data_urls") or []
        matched_news = item.get("matched_news") or []
        matched_focus_keywords = item.get("matched_focus_keywords") or []
        matched_focus_hashtags = item.get("matched_focus_hashtags") or []
        event_keywords = item.get("event_keywords") or []
        relevance_score = item.get("relevance_score", 0)
        relevance_reasons = item.get("relevance_reasons") or []
        source_label = html_lib.escape(f"KOL/{platform}@{account_name}", quote=True)

        images_html = ""
        if media_urls or embedded_media_data_urls or downloaded_media_paths:
            thumb_items = []
            media_count = max(len(media_urls), len(embedded_media_data_urls), len(downloaded_media_paths))
            for media_idx in range(min(media_count, 4)):
                media_url = str(media_urls[media_idx]) if media_idx < len(media_urls) else url
                esc_media = html_lib.escape(media_url, quote=True)
                local_path = str(downloaded_media_paths[media_idx]) if media_idx < len(downloaded_media_paths) else ""
                embedded_data = str(embedded_media_data_urls[media_idx]) if media_idx < len(embedded_media_data_urls) else ""
                img_src_raw = _inline_or_remote_image_src(local_path, media_url, embedded_data)
                img_src = html_lib.escape(img_src_raw, quote=True)
                click_href = img_src_raw if img_src_raw.startswith("data:image/") else url
                thumb_items.append(
                    f'<a class="kol-thumb" href="{html_lib.escape(click_href, quote=True)}" target="_blank" rel="noreferrer">'
                    f'<img src="{img_src}" loading="lazy" alt="{title}"></a>'
                )
            images_html = f'<div class="kol-thumbs">{"".join(thumb_items)}</div>'

        matched_html = ""
        if matched_news:
            lines = []
            for news in matched_news[:3]:
                news_title = html_lib.escape(str(news.get("title", "")).strip())
                news_link = html_lib.escape(str(news.get("link", "")).strip(), quote=True)
                news_source = html_lib.escape(str(news.get("source", "")).strip())
                lines.append(
                    f'<li><a href="{news_link}" target="_blank">{news_title}</a>'
                    f'<span class="kol-match-src">{news_source}</span></li>'
                )
            matched_html = (
                '<div class="kol-matched"><div class="kol-subtitle">关联报道</div>'
                f'<ul>{"".join(lines)}</ul></div>'
            )

        filter_badges = []
        for keyword in matched_focus_keywords[:4]:
            filter_badges.append(f'<span class="kol-filter-tag">关键词：{html_lib.escape(str(keyword))}</span>')
        for tag in matched_focus_hashtags[:4]:
            filter_badges.append(f'<span class="kol-filter-tag">话题：#{html_lib.escape(str(tag))}#</span>')
        for keyword in event_keywords[:3]:
            filter_badges.append(f'<span class="kol-filter-tag kol-filter-tag-soft">事件词：{html_lib.escape(str(keyword))}</span>')
        filters_html = (
            f'<div class="kol-filters">{"".join(filter_badges)}</div>'
            if filter_badges
            else ""
        )
        reason_html = ""
        if relevance_reasons:
            reason_items = "".join(
                f"<li>{html_lib.escape(str(reason))}</li>"
                for reason in relevance_reasons[:3]
            )
            reason_html = f'<ul class="kol-reasons">{reason_items}</ul>'

        cards.append(
            f"""
      <div class="kol-card">
        <div class="kol-card-head">
          <div class="kol-card-meta">
            <span class="kol-rank">#{idx}</span>
            <span class="kol-account">{account_name}</span>
            <span class="kol-platform">{platform}</span>
            <span class="kol-score">传播热度 {score}</span>
            <span class="kol-score kol-score-secondary">相关度 {relevance_score}</span>
          </div>
          <button class="btn-star" onclick="toggleFavorite(this)"
            data-link="{url}" data-title="{title}" data-source="{source_label}">☆</button>
        </div>
        <a class="kol-title" href="{url}" target="_blank">{title}</a>
        <div class="kol-pub">{published_at}</div>
        {filters_html}
        {reason_html}
        {f'<p class="kol-text">{text}</p>' if text else ''}
        {images_html}
        {matched_html}
      </div>"""
        )

    notes_html = ""
    if notes:
        note_lines = "".join(f"<li>{html_lib.escape(str(x))}</li>" for x in notes[:4])
        notes_html = f'<ul class="kol-notes">{note_lines}</ul>'

    count = int(report.get("posts_count") or len(items))
    cards_html = ''.join(cards) if cards else '<div class="kol-empty">当前还没有符合筛选条件的 KOL 内容。</div>'
    return f"""
<div id="kol-section" class="kol-shell is-collapsed">
  <div class="kol-shell-head">
    <div class="kol-shell-title-wrap">
      <h2>KOL 账号信号观察 <span class="kol-count">{count}</span></h2>
      <div class="kol-head-meta">
        <span class="kol-test-badge">测试中</span>
        <span class="kol-head-hint">默认收纳，避免影响主页面阅读</span>
      </div>
    </div>
    <button type="button" class="kol-toggle-btn" aria-expanded="false" onclick="toggleKOLSection(this)">展开</button>
  </div>
  <div class="kol-shell-body">
    <p class="kol-desc">聚合跟踪账号最近公开内容，并结合现有游戏新闻缓存做轻量交叉验证，优先展示命中筛选词的话题内容。</p>
    {notes_html}
    <div class="kol-grid">{cards_html}</div>
  </div>
</div>
"""


def _generate_html(
    news_items: list,
    date_str: str,
    article_analyses: "dict | None" = None,
    results: "list | None" = None,
) -> str:
    """生成本地 HTML 汇总页（Top5 精选 + 按来源分栏新闻列表）。"""
    from collections import defaultdict

    if article_analyses is None:
        article_analyses = {}
    if results is None:
        results = []
    kol_report = _load_kol_report()
    kol_section_html = _build_kol_section_html(kol_report)

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")

    # 把 news_items 映射到全局索引，方便后面查 analysis
    item_index: dict = {}
    for i, item in enumerate(news_items):
        item_index[id(item)] = i

    # 按来源分组（保持原始顺序）
    by_source: dict = defaultdict(list)
    for item in news_items:
        by_source[item.source or "其他"].append(item)

    # ── ① Top5 选题卡片 ───────────────────────────────────
    picks_html = ""
    for r in results:
        angles = "".join(f"<li>{a}</li>" for a in r.angles)
        esc_pick_link  = html_lib.escape(r.source_link or "#", quote=True)
        esc_pick_title = html_lib.escape(r.title)
        link_html = (
            f'<a class="orig-link" href="{esc_pick_link}" target="_blank">查看原文 →</a>'
            if r.source_link else ""
        )
        picks_html += f"""
      <div class="pick" id="pick-{r.rank}">
        <div class="pick-header">
          <h3>No.{r.rank} &nbsp; {r.title} &nbsp; <span class="score">{r.score}/10</span></h3>
          <button class="btn-star pick-star" onclick="toggleFavorite(this)"
            data-link="{esc_pick_link}" data-title="{esc_pick_title}"
            data-source="Top5精选" title="收藏到顶部">☆</button>
        </div>
        <p><strong>选题理由：</strong>{r.reason}</p>
        <ul>{angles}</ul>
        {link_html}
      </div>"""

    # ── ② 按来源分节（预嵌入 AI 分析卡片）──────────────
    # 来源排序：中文媒体 → 英文媒体 → 日文媒体
    _CN_SOURCES = {"3DM", "游民星空", "IT之家", "手游那点事", "游研社"}
    _JP_SOURCES = {"4Gamer"}

    def _source_sort_key(name: str) -> tuple:
        if name in _CN_SOURCES:
            return (0, name)
        if name in _JP_SOURCES:
            return (2, name)
        return (1, name)

    source_sections = ""
    _src_idx = 0
    for source, items in sorted(by_source.items(), key=lambda kv: _source_sort_key(kv[0])):
        _src_idx += 1
        rows = ""
        for item in items:
            title = item.text.split("\n")[0][:150]
            link = item.permalink or "#"
            esc_link = html_lib.escape(link, quote=True)
            esc_title = html_lib.escape(title)

            # 日期标签：Unix 时间戳 → "4月2日"
            try:
                import datetime as _dt
                ts = float(item.timestamp or "0")
                pub = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).astimezone()
                date_badge = f'<span class="date-tag">{pub.month}/{pub.day}</span>'
            except Exception:
                date_badge = ""

            rows += f"""
        <tr data-ts="{item.timestamp or '0'}">
          <td>
            <div class="news-row">
              {date_badge}
              <a class="news-link" href="{esc_link}" target="_blank">{esc_title}</a>
              <div class="btn-group">
                <button class="btn-star" onclick="toggleFavorite(this)"
                  data-link="{esc_link}" data-title="{esc_title}" data-source="{html_lib.escape(source)}">☆</button>
                <button class="btn-analyze" onclick="handleAnalyze(this)" data-title="{esc_title}" data-link="{esc_link}">AI 分析</button>
              </div>
            </div>
            <div class="ai-panel"></div>
          </td>
          <td class="src-tag">{source}</td>
        </tr>"""
        source_sections += f"""
      <h3 class="src-title">{source} <span class="src-count">{len(items)}</span>
        <button class="sort-btn" data-dir="desc" onclick="sortSection(this,'{_src_idx}')">↓ 最新</button>
      </h3>
      <table><tbody id="src-{_src_idx}">{rows}
      </tbody></table>
      <div class="pg-bar" id="pgbar-{_src_idx}"></div>
      <hr class="src-divider">"""

    analyze_api_url = os.environ.get("ANALYZE_API_URL", "")

    html = f"""\ufeff<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>游戏选题日报 · {date_str}</title>
<style>
body {{ font-family: -apple-system, "Helvetica Neue", sans-serif; max-width: 960px; margin: 0 auto; padding: 24px 20px; color: #333; }}
h1 {{ color: #1a73e8; border-bottom: 2px solid #1a73e8; padding-bottom: 8px; margin-bottom: 4px; }}
h2 {{ color: #555; margin-top: 36px; }}
.date-badge {{ font-size: 28px; font-weight: 700; color: #1a73e8; letter-spacing: 1px; display: block; margin: 4px 0 2px; }}
.meta-sub {{ color: #888; font-size: 13px; margin: 0 0 28px; }}
.pick {{ background: #f8f9fa; border-left: 4px solid #1a73e8; padding: 16px; margin: 16px 0; border-radius: 4px; }}
.pick-header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }}
.pick h3 {{ margin: 0 0 8px; font-size: 15px; flex:1; }}
.pick-star {{ flex-shrink:0; font-size:18px; padding:0 4px; border:none; background:transparent; cursor:pointer; color:#bbb; line-height:1.4; transition:color .2s; }}
.pick-star:hover {{ color:#f9a825; }}
.pick-star.starred {{ color:#f9a825; }}
.score {{ background: #1a73e8; color: white; padding: 2px 8px; border-radius: 12px; font-size: 13px; }}
.pick p {{ margin: 6px 0; font-size: 13px; line-height: 1.6; }}
.pick ul {{ margin: 6px 0 10px 18px; font-size: 13px; line-height: 1.7; }}
.orig-link {{ font-size: 12px; color: #1a73e8; text-decoration: none; }}
.orig-link:hover {{ text-decoration: underline; }}
.src-title {{ color: #1a73e8; font-size: 15px; margin: 28px 0 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
.src-count {{ background: #e8f0fe; color: #1a73e8; font-size: 11px; font-weight: 400; padding: 1px 8px; border-radius: 10px; }}
.sort-btn {{ font-size:11px; padding:1px 9px; border:1px solid #c5d1f5; border-radius:10px; background:#fff; color:#1a73e8; cursor:pointer; margin-left:2px; transition:all .15s; }}
.sort-btn:hover {{ background:#e8f0fe; }}
.src-divider {{ border: none; border-top: 1px solid #e0e0e0; margin: 20px 0 0; }}
table {{ width: 100%; border-collapse: collapse; }}
td {{ padding: 7px 10px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }}
.src-tag {{ color: #aaa; font-size: 11px; white-space: nowrap; width: 80px; text-align: right; padding-top: 9px; }}
.news-row {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; }}
.date-tag {{ flex-shrink:0; font-size:11px; color:#aaa; background:#f5f5f5; border-radius:4px; padding:1px 5px; align-self:center; white-space:nowrap; }}
.news-link {{ color: #333; text-decoration: none; font-size: 13px; line-height: 1.5; flex: 1; }}
.news-link:hover {{ color: #1a73e8; }}
.btn-group {{ display:flex; align-items:flex-start; gap:4px; flex-shrink:0; }}
.btn-analyze {{ flex-shrink: 0; align-self: flex-start; margin-top: 1px; font-size: 11px; padding: 2px 9px; border: 1px solid #1a73e8; color: #1a73e8; background: #fff; border-radius: 10px; cursor: pointer; white-space: nowrap; transition: all .2s; }}
.btn-analyze:hover {{ background: #1a73e8; color: #fff; }}
.btn-analyze:disabled {{ border-color: #ccc; color: #999; cursor: default; background: #f8f8f8; }}
.btn-star {{ flex-shrink:0; align-self:flex-start; margin-top:1px; font-size:15px; padding:0 4px; border:none; background:transparent; cursor:pointer; color:#bbb; line-height:1.5; transition:color .2s; }}
.btn-star:hover {{ color:#f9a825; }}
.btn-star.starred {{ color:#f9a825; }}
.ai-panel {{ display: none; margin-top: 4px; }}
.ai-loading {{ color: #1a73e8; font-size: 12px; padding: 6px 0; }}
.ai-card {{ background: #fff; border-left: 4px solid #1a73e8; padding: 12px 14px; border-radius: 4px; animation: fadeIn .3s ease; }}
/* ── 收藏区块 ── */
#fav-section {{ margin:0 0 32px; }}
#fav-section h2 {{ margin-bottom:8px; }}
.fav-card {{ background:#fffde7; border-left:4px solid #f9a825; padding:10px 14px; margin:6px 0; border-radius:4px; display:flex; flex-direction:column; align-items:stretch; gap:6px; }}
.fav-card-row {{ display:flex; align-items:center; gap:10px; }}
.fav-link {{ color:#333; text-decoration:none; font-size:13px; font-weight:500; flex:1; line-height:1.4; }}
.fav-link:hover {{ color:#1a73e8; }}
.fav-src {{ background:#fef3c7; color:#92400e; font-size:11px; padding:1px 8px; border-radius:10px; white-space:nowrap; }}
.btn-unfav {{ font-size:11px; padding:2px 9px; border:1px solid #f9a825; color:#92400e; background:#fff; border-radius:10px; cursor:pointer; white-space:nowrap; transition:all .2s; }}
.btn-unfav:hover {{ background:#f9a825; color:#fff; }}
.kol-count {{ background:#e8f0fe; color:#1a73e8; font-size:12px; padding:2px 8px; border-radius:10px; vertical-align:middle; }}
.kol-desc {{ color:#666; font-size:13px; margin:6px 0 12px; }}
.kol-notes {{ margin:0 0 12px 18px; color:#777; font-size:12px; line-height:1.6; }}
.kol-shell {{ background:linear-gradient(180deg,#fbfdff 0%,#f5f9ff 100%); border:1px solid #dbe7ff; border-radius:14px; padding:18px 18px 16px; margin:10px 0 22px; box-shadow:0 8px 24px rgba(38,78,136,.06); }}
.kol-shell-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:6px; }}
.kol-shell-title-wrap {{ min-width:0; }}
.kol-head-meta {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:6px; }}
.kol-test-badge {{ display:inline-flex; align-items:center; padding:2px 8px; border-radius:999px; background:#fff4d6; color:#9a6700; font-size:11px; font-weight:600; border:1px solid #f2d38b; }}
.kol-head-hint {{ color:#8a94a6; font-size:12px; }}
.kol-toggle-btn {{ flex-shrink:0; border:1px solid #c9dafd; background:#fff; color:#1a73e8; border-radius:999px; padding:5px 12px; font-size:12px; font-weight:600; cursor:pointer; transition:all .18s ease; }}
.kol-toggle-btn:hover {{ background:#eaf2ff; }}
.kol-shell-body {{ margin-top:10px; }}
.kol-shell.is-collapsed .kol-shell-body {{ display:none; }}
.kol-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; align-items:start; }}
.kol-card {{ background:#ffffff; border:1px solid #d9ecff; border-left:4px solid #4c8bf5; border-radius:10px; padding:14px 14px 12px; min-height:100%; box-shadow:0 4px 14px rgba(32,89,167,.05); }}
.kol-card-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:6px; }}
.kol-card-meta {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; color:#557; font-size:11px; }}
.kol-rank {{ background:#1a73e8; color:#fff; border-radius:10px; padding:1px 8px; }}
.kol-account {{ font-weight:600; color:#333; }}
.kol-platform {{ background:#eef3ff; color:#4c65b8; border-radius:10px; padding:1px 8px; text-transform:capitalize; }}
.kol-score {{ background:#fff3cd; color:#8a5b00; border-radius:10px; padding:1px 8px; }}
.kol-score-secondary {{ background:#eef7ff; color:#2d5db3; }}
.kol-title {{ display:block; color:#222; font-size:15px; font-weight:700; text-decoration:none; margin:4px 0 6px; line-height:1.5; }}
.kol-title:hover {{ color:#1a73e8; }}
.kol-pub {{ color:#999; font-size:11px; margin-bottom:6px; }}
.kol-filters {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 10px; }}
.kol-filter-tag {{ font-size:11px; color:#0f5d52; background:#e7f7f3; border:1px solid #c3ebe2; border-radius:999px; padding:2px 8px; }}
.kol-filter-tag-soft {{ color:#355f9d; background:#edf4ff; border-color:#d7e4fb; }}
.kol-reasons {{ margin:0 0 10px 18px; padding:0; color:#667; font-size:12px; line-height:1.55; }}
.kol-reasons li {{ margin-bottom:3px; }}
.kol-text {{ color:#444; font-size:13px; line-height:1.65; margin:0 0 10px; white-space:pre-wrap; display:-webkit-box; -webkit-line-clamp:4; -webkit-box-orient:vertical; overflow:hidden; }}
.kol-thumbs {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:4px 0 10px; }}
.kol-thumb {{ display:block; aspect-ratio:1 / 1; overflow:hidden; border-radius:10px; border:1px solid #dfe8f4; background:#f3f7ff; }}
.kol-thumb img {{ width:100%; height:100%; object-fit:cover; display:block; background:#edf4ff; }}
.kol-matched {{ margin-top:6px; }}
.kol-subtitle {{ color:#4c65b8; font-size:12px; font-weight:600; margin-bottom:4px; }}
.kol-matched ul {{ margin:0 0 0 18px; padding:0; }}
.kol-matched li {{ margin-bottom:4px; font-size:12px; line-height:1.5; }}
.kol-matched a {{ color:#1a73e8; text-decoration:none; }}
.kol-matched a:hover {{ text-decoration:underline; }}
.kol-match-src {{ color:#999; font-size:11px; margin-left:6px; }}
.kol-empty {{ color:#888; font-size:12px; padding:8px 0; }}
@media (max-width: 900px) {{
  .kol-grid {{ grid-template-columns:1fr; }}
}}
@media (max-width: 560px) {{
  .kol-shell {{ padding:14px 12px; }}
  .kol-shell-head {{ flex-direction:column; align-items:flex-start; }}
  .kol-thumbs {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
}}
@keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(-4px); }} to {{ opacity: 1; transform: translateY(0); }} }}
@keyframes blink {{ 0%,100%{{opacity:.3}} 50%{{opacity:1}} }}
.ai-loading-dot {{ display:inline-block; width:6px; height:6px; background:#1a73e8; border-radius:50%; animation:blink 1s infinite; margin-right:6px; }}
/* ── 分页 ── */
.pg-bar {{ display:flex; align-items:center; gap:8px; margin:6px 0 14px; font-size:12px; color:#888; min-height:22px; }}
.pg-btn {{ padding:2px 12px; border:1px solid #ddd; border-radius:10px; background:#fff; cursor:pointer; font-size:12px; color:#555; transition:all .15s; }}
.pg-btn:hover {{ border-color:#1a73e8; color:#1a73e8; }}
.pg-btn:disabled {{ opacity:.35; cursor:default; }}
.pg-info {{ flex:1; text-align:center; }}
/* ── 全文分析按钮 ── */
.btn-full-analyze {{ font-size:11px; padding:2px 9px; border:1px solid #43a047; color:#43a047; background:#fff; border-radius:10px; cursor:pointer; white-space:nowrap; transition:all .2s; flex-shrink:0; }}
.btn-full-analyze:hover {{ background:#43a047; color:#fff; }}
.btn-full-analyze:disabled {{ border-color:#ccc; color:#999; cursor:default; background:#f8f8f8; }}
.btn-research {{ font-size:11px; padding:2px 9px; border:1px solid #6d4aff; color:#6d4aff; background:#fff; border-radius:10px; cursor:pointer; white-space:nowrap; transition:all .2s; flex-shrink:0; }}
.btn-research:hover {{ background:#6d4aff; color:#fff; }}
.btn-research:disabled {{ border-color:#ccc; color:#999; cursor:default; background:#f8f8f8; }}
.fav-full-panel {{ display:none; margin-top:4px; }}
.fav-research-panel {{ display:none; margin-top:4px; }}
/* ── 便利贴按钮 ── */
.btn-notes {{ font-size:14px; padding:1px 5px; border:none; background:transparent; cursor:pointer; color:#ccc; transition:color .2s; flex-shrink:0; border-radius:4px; line-height:1.5; }}
.btn-notes:hover {{ color:#f9a825; background:#fff3cd; }}
/* ── 便利贴浮层面板 ── */
#notes-panel {{ position:fixed; right:0; top:0; width:285px; height:100vh; background:#fffef5; border-left:2px solid #f9a825; box-shadow:-4px 0 20px rgba(0,0,0,.12); z-index:1000; display:flex; flex-direction:column; transform:translateX(110%); transition:transform .25s ease; }}
#notes-panel.open {{ transform:translateX(0); }}
#notes-panel-header {{ display:flex; align-items:flex-start; justify-content:space-between; padding:14px 12px 10px; border-bottom:1px solid #ffe082; background:#fff8e1; gap:8px; }}
#notes-panel-title {{ font-size:12px; font-weight:600; color:#555; line-height:1.5; flex:1; word-break:break-all; }}
#notes-panel-close {{ border:none; background:transparent; font-size:20px; color:#bbb; cursor:pointer; padding:0 2px; line-height:1; flex-shrink:0; }}
#notes-panel-close:hover {{ color:#555; }}
#notes-list {{ flex:1; overflow-y:auto; padding:10px 12px; }}
.note-item {{ background:#fff; border:1px solid #ffe082; border-radius:6px; padding:8px 10px; margin-bottom:8px; }}
.note-text {{ font-size:13px; line-height:1.6; color:#333; white-space:pre-wrap; word-break:break-word; }}
.note-meta {{ font-size:11px; color:#aaa; margin-top:4px; display:flex; justify-content:space-between; align-items:center; }}
.note-del-btn {{ border:none; background:transparent; color:#ccc; cursor:pointer; font-size:11px; padding:0; }}
.note-del-btn:hover {{ color:#e53935; }}
#notes-input-area {{ padding:10px 12px; border-top:1px solid #ffe082; background:#fff8e1; }}
#notes-input {{ width:100%; box-sizing:border-box; border:1px solid #ffe082; border-radius:6px; padding:7px 10px; font-size:13px; resize:none; font-family:inherit; min-height:60px; background:#fff; }}
#notes-input:focus {{ outline:none; border-color:#f9a825; }}
#notes-submit-btn {{ margin-top:6px; width:100%; padding:7px; background:#f9a825; color:#fff; border:none; border-radius:6px; font-size:13px; cursor:pointer; font-weight:500; }}
#notes-submit-btn:hover {{ background:#e6961a; }}
#notes-submit-btn:disabled {{ background:#ccc; cursor:default; }}
</style>
</head>
<body>
<h1>🎮 游戏选题日报</h1>
<span class="date-badge">{date_str}</span>
<p class="meta-sub">共 {len(news_items)} 条新闻 &nbsp;|&nbsp; 来自 {len(by_source)} 个媒体</p>

<div id="fav-section" style="display:none;">
  <h2>⭐ 精选关注</h2>
  <div id="fav-list"></div>
</div>

{kol_section_html}

<h2>📌 AI 精选 Top {len(results)} 选题</h2>
{picks_html}

<h2>📰 全部新闻（按来源分节）</h2>
{source_sections}

<script>
var ANALYZE_API = "{analyze_api_url}";
var FAVORITES_API   = ANALYZE_API ? ANALYZE_API.replace('/analyze', '/favorites')    : '';
var FULL_ANALYZE_API = ANALYZE_API ? ANALYZE_API.replace('/analyze', '/analyze_full') : '';
var NOTES_API        = ANALYZE_API ? ANALYZE_API.replace('/analyze', '/notes')        : '';
var RESEARCH_API     = ANALYZE_API ? ANALYZE_API.replace('/analyze', '/research_topic') : '';

/* ── 收藏功能 ──────────────────────────────────── */
var _favLinks = {{}};  // link → true，用于快速判断是否已收藏
function _analysisCacheKey(title, link) {{
  return (title || '') + '||' + (link || '');
}}

function _renderFavorites(items) {{
  var section = document.getElementById('fav-section');
  var list    = document.getElementById('fav-list');
  _favLinks = {{}};
  (items || []).forEach(function(x) {{
    _favLinks[x.link] = true;
    /* 把 Top5 存储的分析内容预填入缓存，点击"AI 分析"时无需再请求 API */
    if (x.analysis_html && x.title) {{
      _analysisCache[_analysisCacheKey(x.title, x.link)] = x.analysis_html;
    }}
  }});

  // 同步每行的星星状态
  document.querySelectorAll('.btn-star').forEach(function(btn) {{
    var on = !!_favLinks[btn.dataset.link];
    btn.textContent = on ? '⭐' : '☆';
    btn.title = on ? '取消收藏' : '收藏到顶部';
    btn.classList.toggle('starred', on);
  }});

  if (!items || items.length === 0) {{
    section.style.display = 'none';
    return;
  }}
  section.style.display = 'block';
  list.innerHTML = items.map(function(x) {{
    var esc = function(s) {{ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }};
    return '<div class="fav-card">' +
      '<div class="fav-card-row">' +
        '<a class="fav-link" href="' + esc(x.link) + '" target="_blank">' + esc(x.title) + '</a>' +
        '<span class="fav-src">' + esc(x.source || '') + '</span>' +
        '<div style="display:flex;gap:4px;flex-shrink:0;">' +
          '<button class="btn-analyze" onclick="handleAnalyze(this)" data-title="' + esc(x.title) + '" data-link="' + esc(x.link) + '">AI 分析</button>' +
          '<button class="btn-research" onclick="handleResearchTopic(this)" data-title="' + esc(x.title) + '" data-link="' + esc(x.link) + '" data-source="' + esc(x.source || '') + '">资料研究</button>' +
          '<button class="btn-full-analyze" onclick="handleFullAnalyze(this)"' +
            ' data-link="' + esc(x.link) + '"' +
            ' data-title="' + esc(x.title) + '" title="读取全文后 AI 深度分析">全文分析</button>' +
          '<button class="btn-notes" onclick="openNotesPanel(this)"' +
            ' data-link="' + esc(x.link) + '"' +
            ' data-title="' + esc(x.title) + '" title="便利贴备注">📝</button>' +
          '<button class="btn-unfav" onclick="toggleFavorite(this)"' +
            ' data-link="' + esc(x.link) + '"' +
            ' data-title="' + esc(x.title) + '"' +
            ' data-source="' + esc(x.source || '') + '">取消</button>' +
        '</div>' +
      '</div>' +
      '<div class="ai-panel fav-ai-panel" style="display:none;"></div>' +
      '<div class="fav-research-panel" style="display:none;"></div>' +
      '<div class="fav-full-panel" style="display:none;"></div>' +
    '</div>';
  }}).join('');
}}

function toggleFavorite(btn) {{
  if (!FAVORITES_API) return;
  var link   = btn.dataset.link;
  var title  = btn.dataset.title;
  var source = btn.dataset.source || '';

  /* 如果从 Top5 卡片收藏，顺便把已有分析内容打包存入 Gist */
  var analysisHtml = '';
  var pickCard = btn.closest('.pick');
  if (pickCard) {{
    var p = pickCard.querySelector('p');
    var ul = pickCard.querySelector('ul');
    var h3 = pickCard.querySelector('h3');
    var scoreEl = h3 ? h3.querySelector('.score') : null;
    var scoreText = scoreEl ? scoreEl.textContent : '';
    analysisHtml =
      '<div style="background:#fff;border-left:4px solid #1a73e8;padding:12px 14px;border-radius:4px;">' +
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">' +
        '<strong style="font-size:14px;">Top5 精选</strong>' +
        (scoreText ? '<span style="background:#f29900;color:#fff;padding:2px 8px;border-radius:10px;font-size:12px;">' + scoreText + '</span>' : '') +
      '</div>' +
      (p ? '<p style="margin:4px 0;font-size:13px;line-height:1.6;">' + p.innerHTML + '</p>' : '') +
      (ul && ul.children.length ? '<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.7;">' + ul.innerHTML + '</ul>' : '') +
      '</div>';
  }}

  btn.disabled = true;
  fetch(FAVORITES_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title: title, link: link, source: source, analysis_html: analysisHtml}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    btn.disabled = false;
    if (data.error) {{ alert('操作失败：' + data.error); return; }}
    _renderFavorites(data.items || []);
    if (data.warning) {{
      _showToast('⚠️ 收藏仅本次有效，刷新后消失（Render 未配置 Gist 环境变量）');
    }}
  }})
  .catch(function(e) {{
    btn.disabled = false;
    alert('网络错误：' + e.message);
  }});
}}

function _showToast(msg) {{
  var t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);'
    + 'background:#333;color:#fff;padding:8px 16px;border-radius:6px;font-size:13px;'
    + 'z-index:9999;max-width:90%;text-align:center;';
  document.body.appendChild(t);
  setTimeout(function() {{ t.remove(); }}, 5000);
}}

function toggleKOLSection(btn) {{
  var section = document.getElementById('kol-section');
  if (!section) return;
  var collapsed = section.classList.toggle('is-collapsed');
  btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  btn.textContent = collapsed ? '展开' : '收起';
}}

function _loadFavorites() {{
  if (!FAVORITES_API) return;
  fetch(FAVORITES_API)
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{ _renderFavorites(data.items || []); }})
    .catch(function() {{}});
}}

/* ── AI 分析功能 ──────────────────────────────── */
var _analysisCache = {{}};
function handleAnalyze(btn) {{
  var panel;
  var favCard = btn.closest('.fav-card');
  if (favCard) {{
    panel = favCard.querySelector('.fav-ai-panel');
  }} else {{
    panel = btn.closest('.news-row').nextElementSibling;
  }}
  var title = btn.dataset.title || '';
  var link = btn.dataset.link || '';
  var cacheKey = _analysisCacheKey(title, link);
  if (panel.style.display === 'block') {{
    panel.style.display = 'none';
    btn.textContent = 'AI 分析';
    return;
  }}
  if (_analysisCache[cacheKey]) {{
    panel.innerHTML = _analysisCache[cacheKey];
    panel.style.display = 'block';
    btn.textContent = '收起';
    return;
  }}
  if (btn.disabled) return;
  if (!ANALYZE_API) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">⚠️ 实时分析服务未配置</div>';
    panel.style.display = 'block';
    return;
  }}
  btn.disabled = true;
  btn.textContent = '分析中…';
  panel.innerHTML = '<div class="ai-loading"><span class="ai-loading-dot"></span>正在调用 AI 分析，请稍候…</div>';
  panel.style.display = 'block';
  fetch(ANALYZE_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title: title, link: link}})
  }})
  .then(function(res) {{ return res.json(); }})
  .then(function(data) {{
    if (data.error) throw new Error(data.error);
    var html = data.html || '<div style="color:#888;font-size:12px;">（未获得分析结果）</div>';
    if (data.complete !== false) {{
      _analysisCache[cacheKey] = html;
    }}
    panel.innerHTML = html;
    btn.disabled = false;
    btn.textContent = data.complete === false ? 'AI 分析' : '收起';
  }})
  .catch(function(e) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">分析失败：' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = 'AI 分析';
  }});
}}

/* ── 全文分析功能 ─────────────────────────────────── */
var _researchCache = {{}};
function handleResearchTopic(btn) {{
  var favCard = btn.closest('.fav-card');
  if (!favCard) return;
  var panel = favCard.querySelector('.fav-research-panel');
  var link  = btn.dataset.link  || '';
  var title = btn.dataset.title || '';
  var source = btn.dataset.source || '';
  var cacheKey = link || title;

  if (panel.style.display === 'block') {{
    panel.style.display = 'none';
    btn.textContent = '资料研究';
    return;
  }}
  if (_researchCache[cacheKey]) {{
    panel.innerHTML = _researchCache[cacheKey];
    panel.style.display = 'block';
    btn.textContent = '收起研究';
    return;
  }}
  if (btn.disabled) return;
  if (!RESEARCH_API) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">⚠ 实时资料研究服务未配置</div>';
    panel.style.display = 'block';
    return;
  }}
  btn.disabled = true;
  btn.textContent = '研究中...';
  panel.innerHTML = '<div class="ai-loading"><span class="ai-loading-dot"></span>正在生成资料研究包，请稍候...</div>';
  panel.style.display = 'block';
  fetch(RESEARCH_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title: title, link: link, source: source}})
  }})
  .then(function(res) {{ return res.json(); }})
  .then(function(data) {{
    if (data.error) throw new Error(data.error);
    var item = data.item || {{}};
    var html = item.html || '<div style="color:#888;font-size:12px;">（未获得资料研究结果）</div>';
    if (item.html) {{
      _researchCache[cacheKey] = item.html;
    }}
    panel.innerHTML = html;
    panel.style.display = 'block';
    btn.disabled = false;
    btn.textContent = '收起研究';
    if (data.warning) _showToast('⚠ ' + data.warning);
  }})
  .catch(function(e) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">资料研究失败：' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = '资料研究';
  }});
}}

function handleFullAnalyze(btn) {{
  var favCard = btn.closest('.fav-card');
  if (!favCard) return;
  var panel = favCard.querySelector('.fav-full-panel');
  var link  = btn.dataset.link  || '';
  var title = btn.dataset.title || '';

  if (panel.style.display === 'block') {{
    panel.style.display = 'none';
    btn.textContent = '全文分析';
    return;
  }}
  if (btn.disabled) return;
  if (!FULL_ANALYZE_API) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">⚠️ 实时分析服务未配置</div>';
    panel.style.display = 'block';
    return;
  }}
  btn.disabled = true;
  btn.textContent = '读取中…';
  panel.innerHTML = '<div class="ai-loading"><span class="ai-loading-dot"></span>正在读取全文并分析（约15-30秒）…</div>';
  panel.style.display = 'block';
  fetch(FULL_ANALYZE_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{link: link, title: title}})
  }})
  .then(function(res) {{ return res.json(); }})
  .then(function(data) {{
    if (data.error) throw new Error(data.error);
    panel.innerHTML = data.html || '<div style="color:#888;font-size:12px;">（未获得分析结果）</div>';
    btn.disabled = false;
    btn.textContent = data.complete === false ? '全文分析' : '收起全文';
  }})
  .catch(function(e) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">全文分析失败：' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = '全文分析';
  }});
}}

/* ── 便利贴功能 ───────────────────────────────────── */
var _notesCurrentLink  = '';
var _notesCurrentTitle = '';

function openNotesPanel(btn) {{
  _notesCurrentLink  = btn.dataset.link  || '';
  _notesCurrentTitle = btn.dataset.title || '';
  document.getElementById('notes-panel-title').textContent = _notesCurrentTitle;
  document.getElementById('notes-panel').classList.add('open');
  _loadNotesList();
}}

function closeNotesPanel() {{
  document.getElementById('notes-panel').classList.remove('open');
}}

function _loadNotesList() {{
  if (!NOTES_API || !_notesCurrentLink) return;
  var list = document.getElementById('notes-list');
  list.innerHTML = '<div style="color:#aaa;font-size:12px;padding:8px;">加载中…</div>';
  fetch(NOTES_API + '?link=' + encodeURIComponent(_notesCurrentLink))
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.error) {{ list.innerHTML = '<div style="color:#e53935;font-size:12px;padding:8px;">加载失败：' + d.error + '</div>'; return; }}
      _renderNotesList(d.notes || []);
    }})
    .catch(function() {{
      list.innerHTML = '<div style="color:#e53935;font-size:12px;padding:8px;">加载失败，请重试</div>';
    }});
}}

function _escN(s) {{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function _renderNotesList(notes) {{
  var list = document.getElementById('notes-list');
  if (!notes || !notes.length) {{
    list.innerHTML = '<div style="color:#aaa;font-size:12px;padding:8px 0;">暂无备注，添加第一条吧 👇</div>';
    return;
  }}
  list.innerHTML = notes.map(function(n) {{
    var dt = n.created_at ? n.created_at.replace('T',' ').slice(0,16) : '';
    return '<div class="note-item">' +
      '<div class="note-text">' + _escN(n.text) + '</div>' +
      '<div class="note-meta">' + dt +
    ' <button class="note-del-btn" data-nid="' + n.id + '" onclick="_deleteNote(this.dataset.nid)">删除</button>' +
      '</div>' +
    '</div>';
  }}).join('');
}}

function _submitNote() {{
  var input = document.getElementById('notes-input');
  var text  = input.value.trim();
  if (!text || !NOTES_API || !_notesCurrentLink) return;
  var btn = document.getElementById('notes-submit-btn');
  btn.disabled = true;
  fetch(NOTES_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{link: _notesCurrentLink, text: text}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{
    btn.disabled = false;
    if (d.error) {{ alert('备注保存失败：' + d.error); return; }}
    input.value = '';
    _renderNotesList(d.notes || []);
    if (d.warning) _showToast('⚠️ 备注仅本次有效（Gist 未配置）');
  }})
  .catch(function(e) {{
    btn.disabled = false;
    alert('保存失败：' + e.message);
  }});
}}

function _deleteNote(noteId) {{
  if (!NOTES_API || !_notesCurrentLink) return;
  fetch(NOTES_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{link: _notesCurrentLink, note_id: noteId}})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(d) {{ _renderNotesList(d.notes || []); }})
  .catch(function(e) {{ alert('删除失败：' + e.message); }});
}}

/* ── 分页 + 排序功能 ──────────────────────────────── */
(function() {{
  var PAGE_SIZE = 15;

  function initPaginationForTbody(tb) {{
    var barId = tb.id.replace('src-', 'pgbar-');
    var bar = document.getElementById(barId);
    if (!bar) return;
    bar.innerHTML = '';
    var rows = [].slice.call(tb.querySelectorAll('tr'));
    [].forEach.call(rows, function(tr) {{ tr.style.display = ''; }});
    if (rows.length <= PAGE_SIZE) return;
    var total = rows.length;
    var totalPages = Math.ceil(total / PAGE_SIZE);
    var cur = 1;
    function go(p) {{
      cur = p;
      [].forEach.call(tb.querySelectorAll('tr'), function(tr, i) {{
        tr.style.display = (i >= (p - 1) * PAGE_SIZE && i < p * PAGE_SIZE) ? '' : 'none';
      }});
      bar.innerHTML =
        '<button class="pg-btn" id="' + barId + '-p"' + (cur <= 1 ? ' disabled' : '') + '>‹ 上一页</button>' +
        '<span class="pg-info">第 ' + cur + ' / ' + totalPages + ' 页（共 ' + total + ' 条）</span>' +
        '<button class="pg-btn" id="' + barId + '-n"' + (cur >= totalPages ? ' disabled' : '') + '>下一页 ›</button>';
      var bp = document.getElementById(barId + '-p');
      var bn = document.getElementById(barId + '-n');
      if (bp) bp.onclick = function() {{ if (cur > 1) go(cur - 1); }};
      if (bn) bn.onclick = function() {{ if (cur < totalPages) go(cur + 1); }};
    }}
    go(1);
  }}

  /* 按时间戳排序 */
  function sortByTs(tb, dir) {{
    var rows = [].slice.call(tb.querySelectorAll('tr'));
    rows.sort(function(a, b) {{
      var ta = parseFloat(a.dataset.ts || '0');
      var tb2 = parseFloat(b.dataset.ts || '0');
      return dir === 'desc' ? tb2 - ta : ta - tb2;
    }});
    rows.forEach(function(tr) {{ tb.appendChild(tr); }});
  }}

  window.sortSection = function(btn, srcIdx) {{
    var tb = document.getElementById('src-' + srcIdx);
    if (!tb) return;
    var curDir = btn.dataset.dir || 'desc';
    var newDir = curDir === 'desc' ? 'asc' : 'desc';
    sortByTs(tb, newDir);
    btn.dataset.dir = newDir;
    btn.textContent = newDir === 'desc' ? '↓ 最新' : '↑ 最旧';
    initPaginationForTbody(tb);
  }};

  function initPagination() {{
    document.querySelectorAll('tbody[id^="src-"]').forEach(function(tb) {{
      sortByTs(tb, 'desc');  /* 默认最新优先 */
      initPaginationForTbody(tb);
    }});
  }}

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', initPagination);
  }} else {{
    initPagination();
  }}
}})();

document.addEventListener('DOMContentLoaded', function() {{
  _loadFavorites();
  var ta = document.getElementById('notes-input');
  if (ta) {{
    ta.addEventListener('keydown', function(e) {{
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {{ _submitNote(); }}
    }});
  }}
}});
</script>

<!-- ── 便利贴浮层面板（position:fixed，不影响主页面布局）── -->
<div id="notes-panel">
  <div id="notes-panel-header">
    <span id="notes-panel-title">便利贴</span>
    <button id="notes-panel-close" onclick="closeNotesPanel()" title="关闭">×</button>
  </div>
  <div id="notes-list"></div>
  <div id="notes-input-area">
    <textarea id="notes-input" placeholder="添加备注…（Shift+Enter 换行）"></textarea>
    <button id="notes-submit-btn" onclick="_submitNote()">发送备注</button>
  </div>
</div>

</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    return html_path


def _get_share_url(html_path: str) -> str:
    """根据 SHARE_MODE 上传并返回可共享 URL，失败或未配置时返回空字符串。"""
    pages_url = share_url_helper.guess_pages_url()

    if config.SHARE_MODE == "gist":
        gist_url = ""
        try:
            with open(html_path, encoding="utf-8") as f:
                html_content = f.read()
            gist_url = gist_uploader.upload(html_content)
        except Exception as e:
            logger.warning("Gist 上传异常：%s", e)
        return pages_url or gist_url

    return pages_url or ""


def _send_news_list(news_items: list, date_str: str) -> None:
    """以飞书消息形式发送全部新闻标题+链接列表（feishu_msg 模式）。"""
    from collections import defaultdict
    import feishu_sender as fs

    by_source: dict = defaultdict(list)
    for item in news_items:
        by_source[item.source or "其他"].append(item)

    lines = [f"📰 今日全部新闻 · {date_str}（共 {len(news_items)} 条）\n"]
    for source, items in sorted(by_source.items()):
        lines.append(f"【{source}】")
        for item in items:
            title = item.text.split("\n")[0][:80]
            link = item.permalink or ""
            lines.append(f"• {title}" + (f"\n  {link}" if link else ""))
        lines.append("")

    text = "\n".join(lines)
    payload = {"msg_type": "text", "content": {"text": text}}
    fs._post(payload)
    logger.info("全量新闻列表已发送到飞书")


if __name__ == "__main__":
    main()
