"""
本地预览服务器
─────────────────────────────────────────────────────────────
由 scheduler.py --serve 自动启动，也可单独运行：
    python server.py

GET  /               → 提供 latest_news.html
GET  /favorites      → 返回当前收藏列表 {items: [...]}
POST /analyze        → 接收 {title}，调用 LLM，返回 {html}
POST /favorites      → 切换收藏状态，返回最新 {action, items}
OPTIONS *            → CORS 预检
"""
from __future__ import annotations

import datetime
import json
import os
import re
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

import config

# ── GitHub Gist（用于持久化收藏列表） ─────────────────────
_GIST_ID: str = os.environ.get("GITHUB_GIST_ID", "")
_GH_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
_FAV_FILENAME = "favorites.json"


def _gist_read() -> dict:
    """从 GitHub Gist 读取 favorites.json，返回 {items:[...]}。"""
    if not _GIST_ID or not _GH_TOKEN:
        return {"items": []}
    try:
        req = urllib.request.Request(
            f"https://api.github.com/gists/{_GIST_ID}",
            headers={
                "Authorization": f"token {_GH_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "ai-news-bot",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        files = data.get("files", {})
        if _FAV_FILENAME in files:
            return json.loads(files[_FAV_FILENAME].get("content", "{}") or "{}")
        return {"items": []}
    except Exception:
        return {"items": []}


def _gist_write(favorites: dict) -> None:
    """将 favorites.json 写回 GitHub Gist（PATCH）。未配置时抛出 RuntimeError。"""
    if not _GIST_ID or not _GH_TOKEN:
        raise RuntimeError("Gist 未配置（缺少 GITHUB_GIST_ID / GITHUB_TOKEN 环境变量）")
    payload = json.dumps({
        "files": {
            _FAV_FILENAME: {
                "content": json.dumps(favorites, ensure_ascii=False, indent=2)
            }
        }
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{_GIST_ID}",
        data=payload,
        method="PATCH",
        headers={
            "Authorization": f"token {_GH_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ai-news-bot",
        },
    )
    urllib.request.urlopen(req, timeout=10)

PORT = 8765
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")

def _build_card_html(
    judgment: str,
    score: str,
    analysis: str,
    angles: list[str],
    suggest_title: str = "",
) -> str:
    """把解析好的字段拼成分析卡片 HTML（支持深度分析格式）。"""
    try:
        score_n = int(re.search(r"\d+", score).group()) if score else 0  # type: ignore[union-attr]
    except Exception:
        score_n = 0
    score_color = "#d93025" if score_n >= 8 else "#f29900" if score_n >= 6 else "#888"

    angles_html = ""
    if angles:
        items_html = "".join(f"<li>{a.strip()}</li>" for a in angles if a.strip())
        angles_html = (
            f'<p style="margin:6px 0 2px;font-size:12px;color:#888;font-weight:600;">'
            f'写作角度</p>'
            f'<ul style="margin:0 0 0 16px;font-size:13px;line-height:1.7;">'
            f"{items_html}</ul>"
        )

    suggest_html = ""
    if suggest_title:
        suggest_html = (
            f'<p style="margin:8px 0 0;font-size:12px;color:#888;font-weight:600;">建议标题</p>'
            f'<p style="margin:2px 0;font-size:13px;color:#1a73e8;font-style:italic;">'
            f'{suggest_title}</p>'
        )

    return (
        '<div style="background:#fff;border-left:4px solid #1a73e8;'
        'padding:12px 14px;border-radius:4px;">'
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;">'
        f'<strong style="font-size:14px;">{judgment}</strong>'
        f'<span style="background:{score_color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:12px;">{score_n}/10</span>'
        "</div>"
        + (
            f'<p style="margin:4px 0;font-size:13px;line-height:1.6;">'
            f"<strong>价值分析：</strong>{analysis}</p>"
            if analysis
            else ""
        )
        + angles_html
        + suggest_html
        + "</div>"
    )


def _parse_llm_response(content: str) -> str:
    """
    解析 LLM 深度分析格式，容错处理各种偏差（含多行续行），返回卡片 HTML。

    支持的字段：判断、评分、价值分析、角度一/二/三、建议标题
    兼容旧格式字段：理由、角度
    """
    PREFIXES = [
        ("判断：", "j"), ("判断:", "j"),
        ("评分：", "s"), ("评分:", "s"),
        ("价值分析：", "r"), ("价值分析:", "r"),
        ("理由：", "r"), ("理由:", "r"),   # 旧格式兼容
        ("角度一：", "a1"), ("角度一:", "a1"),
        ("角度二：", "a2"), ("角度二:", "a2"),
        ("角度三：", "a3"), ("角度三:", "a3"),
        ("角度：", "a0"), ("角度:", "a0"),  # 旧格式兼容
        ("建议标题：", "t"), ("建议标题:", "t"),
    ]

    fields: dict[str, list[str]] = {}
    current_key: str | None = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = False
        for prefix, key in PREFIXES:
            if line.startswith(prefix):
                current_key = key
                fields.setdefault(key, [])
                fields[key].append(line[len(prefix):].strip())
                matched = True
                break
        if not matched and current_key is not None:
            fields[current_key].append(line)

    def _get(key: str) -> str:
        return " ".join(fields.get(key, [])).strip()

    judgment = _get("j")
    score = _get("s")
    analysis = _get("r")

    # 角度：新格式三条独立 > 旧格式管道分隔
    angles: list[str] = []
    for k in ("a1", "a2", "a3"):
        v = _get(k)
        if v:
            angles.append(v)
    if not angles:
        raw_a = _get("a0")
        for sep in ("|", "｜", "/"):
            if sep in raw_a:
                angles = [v.strip() for v in raw_a.split(sep)]
                break
        else:
            if raw_a:
                angles = [raw_a]

    suggest_title = _get("t")

    if not judgment and not analysis:
        safe = content.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        return f'<div style="font-size:13px;line-height:1.7;">{safe}</div>'

    return _build_card_html(judgment, score, analysis, angles, suggest_title)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默访问日志

    # ── GET ──────────────────────────────────────────
    def do_GET(self):
        if self.path in ("/", "/index.html", "/latest_news.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self._cors_headers()
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._json_error(404, "报告文件不存在，请先运行 python scheduler.py --now")
        elif self.path == "/favorites":
            favorites = _gist_read()
            self._json_ok(favorites)
        else:
            self._json_error(404, "Not Found")

    # ── OPTIONS：CORS 预检 ─────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ── POST ─────────────────────────────────────────
    def do_POST(self):
        if self.path == "/analyze":
            self._handle_analyze()
        elif self.path == "/favorites":
            self._handle_favorites()
        else:
            self._json_error(404, "Not Found")

    # ── /analyze：调用 LLM，返回 HTML 分析卡片 ───────
    def _handle_analyze(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            title = body.get("title", "（未提供标题）")
        except Exception as e:
            self._json_error(400, f"请求格式错误：{e}")
            return

        # 复用与 Top5 相同的评分标准，要求深度分析
        system_prompt = (
            "你是一位拥有10年经验的资深游戏媒体编辑，擅长判断哪些游戏新闻最值得深度报道。\n\n"
            "【评分标准（严格遵守）】\n"
            "- 3-4分：版本更新/活动通知/小体量资讯，仅对垂直圈层用户有参考价值，无法出圈\n"
            "- 5-6分：有一定讨论度的行业动态，但深度或受众有限，可作为配稿参考\n"
            "- 7-8分：话题热度或内容深度明显突出，适合大多数玩家读者，值得写稿\n"
            "- 9-10分：多个维度同时突出、极易引发广泛讨论的重大事件，需极其严格，每天不超过2条\n"
            "大多数新闻应落在 5-7 分区间，打 8 分以上需要真正有过人之处。"
        )
        user_prompt = (
            f"请对以下游戏新闻标题进行深度选题分析：\n标题：{title}\n\n"
            "严格按如下格式输出，不要任何额外文字：\n"
            "判断：适合/可参考/不适合（三选一）\n"
            "评分：X/10\n"
            "价值分析：2-3句话，结合话题热度、内容深度、时效性、受众共鸣综合说明\n"
            "角度一：[角度名称]——[2句话说明如何切入及独特之处]\n"
            "角度二：[角度名称]——[2句话说明如何切入及独特之处]\n"
            "角度三：[角度名称]——[2句话说明如何切入及独特之处]\n"
            "建议标题：一个吸引眼球、适合游戏媒体读者的文章标题"
        )

        req_data = json.dumps({
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 1500,
        }).encode("utf-8")

        req = urllib.request.Request(
            config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.LLM_API_KEY}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            llm_text = result["choices"][0]["message"]["content"]
            card_html = _parse_llm_response(llm_text)
            self._json_ok({"html": card_html})
        except Exception as e:
            self._json_error(500, f"LLM 调用失败：{e}")

    # ── /favorites：读写收藏列表 ────────────────────
    def _handle_favorites(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            title = body.get("title", "")
            link = body.get("link", "")
            source = body.get("source", "")
        except Exception as e:
            self._json_error(400, f"请求格式错误：{e}")
            return

        if not link:
            self._json_error(400, "缺少 link 字段")
            return

        favorites = _gist_read()
        items: list = favorites.get("items", [])

        # 查找是否已收藏
        idx = next((i for i, x in enumerate(items) if x.get("link") == link), None)
        if idx is not None:
            items.pop(idx)
            action = "removed"
        else:
            items.insert(0, {
                "title": title,
                "link": link,
                "source": source,
                "added_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
            action = "added"

        favorites["items"] = items
        warning = ""
        try:
            _gist_write(favorites)
        except RuntimeError as e:
            # Gist 未配置：仅本次内存有效，刷新后丢失
            warning = str(e)
        except Exception as e:
            self._json_error(500, f"收藏保存失败：{e}")
            return

        resp: dict = {"action": action, "items": items}
        if warning:
            resp["warning"] = warning
        self._json_ok(resp)

    # ── 辅助方法 ──────────────────────────────────────
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_ok(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code: int, msg: str):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)


def start(open_browser: bool = True) -> None:
    """启动服务器（可从外部调用，用于集成进 scheduler.py）。"""
    url = f"http://localhost:{PORT}"
    server = ThreadingHTTPServer(("localhost", PORT), Handler)

    print(f"本地服务器已启动：{url}")
    if open_browser:
        print("正在自动打开浏览器…")
        Timer(1.0, lambda: webbrowser.open(url)).start()
    print("按 Ctrl+C 停止服务器\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止。")


if __name__ == "__main__":
    start(open_browser=True)
