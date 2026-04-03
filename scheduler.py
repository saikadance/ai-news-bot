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

    # 4. 自动 LLM 分析已永久关闭（成本过高，改为页面手动点击触发）
    results: list = []

    # 5. 单篇预分析已永久关闭——HTML 页面改为点击按钮实时调用 Render API
    article_analyses = analysis_cache.to_index_map(all_items, acache, {})

    # 6. 热点聚类、社交热搜均已关闭，不做任何 LLM 调用

    # 7. 生成本地 HTML 汇总页（用完整 all_items）
    html_path = _generate_html(all_items, date_str, article_analyses)
    logger.info("本地新闻汇总页已生成：%s（共 %d 篇文章）", html_path, len(all_items))

    # 9. 根据 SHARE_MODE 生成可共享链接
    share_url = _get_share_url(html_path)

    # 10. 推送飞书
    if not new_items:
        logger.info("今日无新增文章，跳过飞书推送")
        return

    # 先写 url_cache，防止多次触发时重复推送
    url_cache.save(new_items, ucache)

    feishu_sender.send_report(
        [], date_str, len(new_items), share_url or html_path
    )


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
    date_str: str,
    article_analyses: "dict | None" = None,
) -> str:
    """生成本地 HTML 汇总页（所有自动分析已关闭，仅展示新闻列表）。"""
    from collections import defaultdict

    if article_analyses is None:
        article_analyses = {}

    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")

    # 把 news_items 映射到全局索引，方便后面查 analysis
    item_index: dict = {}
    for i, item in enumerate(news_items):
        item_index[id(item)] = i

    # 按来源分组（保持原始顺序）
    by_source: dict = defaultdict(list)
    for item in news_items:
        by_source[item.source or "其他"].append(item)

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
/* ── 分页 ── */
.pg-bar {{ display:flex; align-items:center; gap:8px; margin:6px 0 14px; font-size:12px; color:#888; min-height:22px; }}
.pg-btn {{ padding:2px 12px; border:1px solid #ddd; border-radius:10px; background:#fff; cursor:pointer; font-size:12px; color:#555; transition:all .15s; }}
.pg-btn:hover {{ border-color:#1a73e8; color:#1a73e8; }}
.pg-btn:disabled {{ opacity:.35; cursor:default; }}
.pg-info {{ flex:1; text-align:center; }}
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

/* ── 分页功能 ──────────────────────────────── */
(function() {{
  var PAGE_SIZE = 15;
  function initPagination() {{
    var tbodies = document.querySelectorAll('tbody[id^="src-"]');
    [].forEach.call(tbodies, function(tb) {{
      var rows = tb.querySelectorAll('tr');
      if (rows.length <= PAGE_SIZE) return;
      var total = rows.length;
      var totalPages = Math.ceil(total / PAGE_SIZE);
      var barId = tb.id.replace('src-', 'pgbar-');
      var bar = document.getElementById(barId);
      if (!bar) return;
      var cur = 1;
      function go(p) {{
        cur = p;
        [].forEach.call(rows, function(tr, i) {{
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
    }});
  }}
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', initPagination);
  }} else {{
    initPagination();
  }}
}})();

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
