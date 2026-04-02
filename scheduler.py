"""
主入口：整合 news_fetcher → llm_analyzer → feishu_sender，支持：
  - 立即运行一次（python scheduler.py --now）
  - 每日定时运行（python scheduler.py，默认按 .env 中的 SCHEDULE_TIME）
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import sys
from datetime import datetime

import schedule
import time

import config
import news_fetcher
import llm_analyzer
import feishu_sender
import url_cache
import gist_uploader
import topic_clusterer
import social_fetcher
import analysis_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_once() -> None:
    """执行一次完整的「拉取 → 分析 → 推送」流程。"""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    logger.info("=" * 60)
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
    logger.info("从分析缓存恢复 %d 篇历史文章", len(cached_items))

    # all_items = 缓存历史文章 + 本次 RSS 全量（去重合并，按时间戳排序）
    all_items = analysis_cache.merge_items(cached_items, rss_items)
    logger.info("合并后共 %d 篇文章（HTML 展示 + 热点聚类用）", len(all_items))

    if not all_items:
        logger.warning("没有任何文章（RSS 和缓存均为空），跳过本次推送")
        feishu_sender.send_report([], date_str, 0)
        return

    # 4. 用今日新增文章做 Top5 LLM 分析（飞书推送用）
    results = []
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
    else:
        logger.info("今日无新增文章，跳过 Top5 分析，仅刷新 HTML 报告")

    # 5. 单篇预分析已永久关闭——HTML 页面改为点击按钮实时调用 Render API
    #    （批量分析每次最多消耗数百次 LLM 调用，成本极高，不适合云端定时任务）
    article_analyses = analysis_cache.to_index_map(all_items, acache, {})

    # 6. 热点聚类 + 深度分析
    #    需要把全部文章标题发给 LLM（约 7000-10000 token/次），成本较高。
    #    由 CLUSTER_ENABLED 环境变量控制，默认关闭。
    _CLUSTER_ENABLED = os.environ.get("CLUSTER_ENABLED", "false").lower() in ("1", "true", "yes")
    clusters: list = []
    if _CLUSTER_ENABLED:
        clusters = topic_clusterer.cluster_news(all_items)
        topic_clusterer.analyze_clusters(clusters)
        logger.info("热点聚类完成：%d 个热点", len(clusters))
    else:
        logger.info("热点聚类已关闭（CLUSTER_ENABLED=false），跳过以节省 token")

    # 7. 抓取社交热搜
    social_hots = social_fetcher.fetch_all_social()
    cross_matches = social_fetcher.cross_validate(social_hots, all_items)

    # 8. 生成本地 HTML 汇总页（用完整 all_items）
    html_path = _generate_html(
        all_items, results, date_str, article_analyses,
        clusters=clusters,
        social_hots=social_hots,
        cross_matches=cross_matches,
    )
    logger.info("本地新闻汇总页已生成：%s（共 %d 篇文章）", html_path, len(all_items))

    # 9. 根据 SHARE_MODE 生成可共享链接
    share_url = _get_share_url(html_path)

    # 10. 推送飞书（今日新增文章数量）
    success = feishu_sender.send_report(
        results, date_str, len(new_items), share_url or html_path
    )
    if success:
        logger.info("本次选题报告推送完成，共 %d 条建议", len(results))
        # 推送成功后才写入 url_cache，避免推送失败时漏掉文章
        url_cache.save(new_items, ucache)
        if config.SHARE_MODE == "feishu_msg":
            _send_news_list(new_items, date_str)
    else:
        logger.error("飞书推送失败，请检查 Webhook 配置")


def main() -> None:
    parser = argparse.ArgumentParser(description="游戏新闻选题 Bot")
    parser.add_argument(
        "--now",
        action="store_true",
        help="立即运行一次，不等待定时调度",
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
        run_once()
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


def _generate_html(
    news_items: list,
    results: list,
    date_str: str,
    article_analyses: "dict | None" = None,
    clusters: "list | None" = None,
    social_hots: "list | None" = None,
    cross_matches: "dict | None" = None,
) -> str:
    """生成本地 HTML 汇总页：热点聚焦 + Top选题 + 热搜 + 按来源分栏新闻。"""
    from collections import defaultdict

    if article_analyses is None:
        article_analyses = {}
    if clusters is None:
        clusters = []
    if social_hots is None:
        social_hots = []
    if cross_matches is None:
        cross_matches = {}

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")

    # 把 news_items 映射到全局索引，方便后面查 analysis
    item_index: dict = {}
    for i, item in enumerate(news_items):
        item_index[id(item)] = i

    # 按来源分组（保持原始顺序）
    by_source: dict = defaultdict(list)
    for item in news_items:
        by_source[item.source or "其他"].append(item)

    # ── ① 热点聚焦区块 ────────────────────────────────────
    clusters_html = ""
    if clusters:
        cluster_cards = ""
        for cl in clusters:
            # 来源 badges
            src_badges = " ".join(
                f'<span style="display:inline-block;background:#fce8e6;color:#d93025;'
                f'font-size:11px;padding:1px 8px;border-radius:10px;margin:2px;">'
                f'{html_lib.escape(s)}</span>'
                for s in sorted(cl.sources)
            )
            # 相关文章链接列表
            art_links = "".join(
                f'<li style="margin:3px 0;font-size:12px;">'
                f'<a href="{html_lib.escape(art.permalink or "#")}" target="_blank" '
                f'style="color:#555;text-decoration:none;">'
                f'[{html_lib.escape(art.source)}] {html_lib.escape(art.text.split(chr(10))[0][:80])}'
                f'</a></li>'
                for art in cl.articles
            )
            # 深度分析内容（默认收起）
            analysis_content = cl.deep_html or '<div style="color:#aaa;font-size:12px;">暂无深度分析</div>'
            cluster_cards += f"""
      <div style="background:#fff5f5;border-left:4px solid #d93025;padding:14px 16px;
                  margin:12px 0;border-radius:4px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap;">
          <span style="font-size:16px;font-weight:700;color:#d93025;">
            🔥 {html_lib.escape(cl.keyword)}</span>
          <span style="color:#888;font-size:12px;">{len(cl.articles)}篇报道 · {len(cl.sources)}家媒体</span>
        </div>
        <div style="margin-bottom:8px;">{src_badges}</div>
        <ul style="margin:0 0 10px 16px;padding:0;">{art_links}</ul>
        <button onclick="toggleCluster(this)"
          style="font-size:12px;padding:3px 12px;border:1px solid #d93025;
                 color:#d93025;background:#fff;border-radius:10px;cursor:pointer;">
          展开深度分析
        </button>
        <div class="cluster-panel" style="display:none;margin-top:10px;">
          {analysis_content}
        </div>
      </div>"""

        clusters_html = f"""
<h2>🔥 多媒体热点聚焦</h2>
<p style="color:#888;font-size:13px;margin:-8px 0 12px;">以下话题被 2+ 家媒体同时报道，值得重点关注</p>
{cluster_cards}"""

    # ── ② Top 选题卡片 ──────────────────────────────────
    picks_html = ""
    for r in results:
        angles = "".join(f"<li>{a}</li>" for a in r.angles)
        link_html = f'<a class="orig-link" href="{r.source_link}" target="_blank">查看原文 →</a>' if r.source_link else ""
        picks_html += f"""
      <div class="pick">
        <h3>No.{r.rank} &nbsp; {r.title} &nbsp; <span class="score">{r.score}/10</span></h3>
        <p><strong>选题理由：</strong>{r.reason}</p>
        <ul>{angles}</ul>
        {link_html}
      </div>"""

    # ── ③ 社交热搜区块 ─────────────────────────────────
    social_html = ""
    if social_hots:
        platform_cols = ""
        for hot in social_hots:
            if hot.error:
                col_content = f'<p style="color:#aaa;font-size:12px;padding:8px 0;">获取失败：{html_lib.escape(hot.error[:60])}</p>'
            else:
                items_html = ""
                for i, topic in enumerate(hot.topics, 1):
                    matched = cross_matches.get(topic, [])
                    badge = ""
                    if matched:
                        link, url = matched[0][0], matched[0][1]
                        badge = (
                            f' <a href="{html_lib.escape(url)}" target="_blank" '
                            f'style="font-size:10px;background:#e8f0fe;color:#1a73e8;'
                            f'padding:1px 6px;border-radius:8px;text-decoration:none;'
                            f'white-space:nowrap;">有相关报道</a>'
                        )
                    rank_color = "#d93025" if i <= 3 else "#f29900" if i <= 7 else "#888"
                    items_html += (
                        f'<div style="padding:5px 0;border-bottom:1px solid #f5f5f5;'
                        f'display:flex;align-items:center;gap:6px;">'
                        f'<span style="color:{rank_color};font-size:12px;font-weight:700;'
                        f'min-width:18px;">{i}</span>'
                        f'<span style="font-size:13px;flex:1;">{html_lib.escape(topic)}</span>'
                        f'{badge}</div>'
                    )
                ts = hot.fetched_at or ""
                col_content = f'<div style="font-size:11px;color:#aaa;margin-bottom:6px;">更新于 {ts}</div>{items_html}'

            platform_cols += (
                f'<div style="flex:1;min-width:180px;background:#fff;border-radius:8px;'
                f'padding:12px 14px;box-shadow:0 1px 4px rgba(0,0,0,.06);">'
                f'<div style="font-size:14px;font-weight:700;margin-bottom:8px;">{html_lib.escape(hot.platform)}</div>'
                f'{col_content}</div>'
            )

        social_html = f"""
<h2>🌐 今日热搜</h2>
<p style="color:#888;font-size:13px;margin:-8px 0 12px;">蓝色标注表示有对应游戏新闻报道，可交叉验证选题价值</p>
<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;">
{platform_cols}
</div>"""

    # ── ④ 按来源分节（预嵌入 AI 分析卡片）──────────────
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
    for source, items in sorted(by_source.items(), key=lambda kv: _source_sort_key(kv[0])):
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
        <tr>
          <td>
            <div class="news-row">
              {date_badge}
              <a class="news-link" href="{esc_link}" target="_blank">{esc_title}</a>
              <div class="btn-group">
                <button class="btn-star" onclick="toggleFavorite(this)"
                  data-link="{esc_link}" data-title="{esc_title}" data-source="{html_lib.escape(source)}">☆</button>
                <button class="btn-analyze" onclick="handleAnalyze(this)" data-title="{esc_title}">AI 分析</button>
              </div>
            </div>
            <div class="ai-panel"></div>
          </td>
          <td class="src-tag">{source}</td>
        </tr>"""
        source_sections += f"""
      <h3 class="src-title">{source} <span class="src-count">{len(items)}</span></h3>
      <table><tbody>{rows}
      </tbody></table>
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
.pick h3 {{ margin: 0 0 8px; font-size: 15px; }}
.score {{ background: #1a73e8; color: white; padding: 2px 8px; border-radius: 12px; font-size: 13px; }}
.pick p {{ margin: 6px 0; font-size: 13px; line-height: 1.6; }}
.pick ul {{ margin: 6px 0 10px 18px; font-size: 13px; line-height: 1.7; }}
.orig-link {{ font-size: 12px; color: #1a73e8; text-decoration: none; }}
.orig-link:hover {{ text-decoration: underline; }}
.src-title {{ color: #1a73e8; font-size: 15px; margin: 28px 0 6px; display: flex; align-items: center; gap: 8px; }}
.src-count {{ background: #e8f0fe; color: #1a73e8; font-size: 11px; font-weight: 400; padding: 1px 8px; border-radius: 10px; }}
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
.fav-card {{ background:#fffde7; border-left:4px solid #f9a825; padding:10px 14px; margin:6px 0; border-radius:4px; display:flex; align-items:center; gap:10px; }}
.fav-link {{ color:#333; text-decoration:none; font-size:13px; font-weight:500; flex:1; line-height:1.4; }}
.fav-link:hover {{ color:#1a73e8; }}
.fav-src {{ background:#fef3c7; color:#92400e; font-size:11px; padding:1px 8px; border-radius:10px; white-space:nowrap; }}
.btn-unfav {{ font-size:11px; padding:2px 9px; border:1px solid #f9a825; color:#92400e; background:#fff; border-radius:10px; cursor:pointer; white-space:nowrap; transition:all .2s; }}
.btn-unfav:hover {{ background:#f9a825; color:#fff; }}
@keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(-4px); }} to {{ opacity: 1; transform: translateY(0); }} }}
@keyframes blink {{ 0%,100%{{opacity:.3}} 50%{{opacity:1}} }}
.ai-loading-dot {{ display:inline-block; width:6px; height:6px; background:#1a73e8; border-radius:50%; animation:blink 1s infinite; margin-right:6px; }}
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

<h2>📌 AI 精选 Top {len(results)} 选题</h2>
{picks_html}

{social_html}

<h2>📰 全部新闻（按来源分节）</h2>
{source_sections}

<script>
var ANALYZE_API = "{analyze_api_url}";
var FAVORITES_API = ANALYZE_API ? ANALYZE_API.replace('/analyze', '/favorites') : '';

/* ── 收藏功能 ──────────────────────────────────── */
var _favLinks = {{}};  // link → true，用于快速判断是否已收藏

function _renderFavorites(items) {{
  var section = document.getElementById('fav-section');
  var list    = document.getElementById('fav-list');
  _favLinks = {{}};
  (items || []).forEach(function(x) {{ _favLinks[x.link] = true; }});

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
    var esc = function(s) {{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }};
    return '<div class="fav-card">' +
      '<a class="fav-link" href="' + esc(x.link) + '" target="_blank">' + esc(x.title) + '</a>' +
      '<span class="fav-src">' + esc(x.source || '') + '</span>' +
      '<button class="btn-unfav" onclick="toggleFavorite(this)"' +
        ' data-link="' + esc(x.link) + '"' +
        ' data-title="' + esc(x.title) + '"' +
        ' data-source="' + esc(x.source || '') + '">取消</button>' +
    '</div>';
  }}).join('');
}}

function toggleFavorite(btn) {{
  if (!FAVORITES_API) return;
  var link   = btn.dataset.link;
  var title  = btn.dataset.title;
  var source = btn.dataset.source || '';
  btn.disabled = true;
  fetch(FAVORITES_API, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title: title, link: link, source: source}})
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
  var panel = btn.closest('.news-row').nextElementSibling;
  var title = btn.dataset.title || '';
  if (panel.style.display === 'block') {{
    panel.style.display = 'none';
    btn.textContent = 'AI 分析';
    return;
  }}
  if (_analysisCache[title]) {{
    panel.innerHTML = _analysisCache[title];
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
    body: JSON.stringify({{title: title}})
  }})
  .then(function(res) {{ return res.json(); }})
  .then(function(data) {{
    if (data.error) throw new Error(data.error);
    var html = data.html || '<div style="color:#888;font-size:12px;">（未获得分析结果）</div>';
    _analysisCache[title] = html;
    panel.innerHTML = html;
    btn.disabled = false;
    btn.textContent = '收起';
  }})
  .catch(function(e) {{
    panel.innerHTML = '<div style="color:#e53935;font-size:12px;padding:6px 0;">分析失败：' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = 'AI 分析';
  }});
}}

document.addEventListener('DOMContentLoaded', _loadFavorites);
</script>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    return html_path


def _get_share_url(html_path: str) -> str:
    """根据 SHARE_MODE 上传并返回可共享 URL，失败或未配置时返回空字符串。"""
    if config.SHARE_MODE == "gist":
        try:
            with open(html_path, encoding="utf-8") as f:
                html_content = f.read()
            return gist_uploader.upload(html_content)
        except Exception as e:
            logger.warning("Gist 上传异常：%s", e)
    return ""


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
