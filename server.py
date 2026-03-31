"""
本地预览服务器
─────────────────────────────────────────────────────────────
由 scheduler.py --serve 自动启动，也可单独运行：
    python server.py

GET  /               → 提供 latest_news.html
POST /analyze        → 接收 {title}，调用 LLM，返回 {html}
OPTIONS /analyze     → CORS 预检
"""
from __future__ import annotations

import json
import os
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

import config

PORT = 8765
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")


def _build_card_html(judgment: str, score: str, reason: str, angles: list[str]) -> str:
    """把解析好的字段拼成与 Top5 风格一致的卡片 HTML。"""
    try:
        score_n = int(score)
    except ValueError:
        score_n = 0
    score_color = "#d93025" if score_n >= 8 else "#f29900" if score_n >= 6 else "#888"
    angles_html = "".join(f"<li>{a.strip()}</li>" for a in angles if a.strip())

    return (
        '<div style="background:#fff;border-left:4px solid #1a73e8;'
        'padding:12px 14px;border-radius:4px;">'
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;">'
        f'<strong style="font-size:14px;">{judgment}</strong>'
        f'<span style="background:{score_color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:12px;">{score_n}/10</span>'
        "</div>"
        f'<p style="margin:4px 0;font-size:13px;line-height:1.6;">'
        f"<strong>选题理由：</strong>{reason}</p>"
        + (
            f'<ul style="margin:6px 0 0 18px;font-size:13px;line-height:1.7;">'
            f"{angles_html}</ul>"
            if angles_html
            else ""
        )
        + "</div>"
    )


def _parse_llm_response(content: str) -> str:
    """
    解析 LLM 返回的固定4行纯文本格式，容错处理各种偏差，
    返回现成的卡片 HTML。
    """
    judgment = score = reason = ""
    angles: list[str] = []

    for line in content.splitlines():
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
                    judgment = val
                elif key == "s":
                    # 只取开头的数字
                    score = "".join(c for c in val if c.isdigit())[:2]
                elif key == "r":
                    reason = val
                elif key == "a":
                    # 支持 | 或 / 或换行分隔多条角度
                    for sep in ("|", "｜", "/"):
                        if sep in val:
                            angles = [v.strip() for v in val.split(sep)]
                            break
                    else:
                        angles = [val]
                break

    # 如果解析完全失败，直接把原始文本展示出来
    if not judgment and not reason:
        safe = content.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        return f'<div style="font-size:13px;line-height:1.7;">{safe}</div>'

    return _build_card_html(judgment, score, reason, angles)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默访问日志

    # ── GET：提供 HTML 页面 ────────────────────────────
    def do_GET(self):
        if self.path in ("/", "/index.html", "/latest_news.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._json_error(404, "报告文件不存在，请先运行 python scheduler.py --now")
        else:
            self._json_error(404, "Not Found")

    # ── OPTIONS：CORS 预检 ─────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    # ── POST /analyze：调用 LLM，返回 HTML ───────────
    def do_POST(self):
        if self.path != "/analyze":
            self._json_error(404, "Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            title = body.get("title", "（未提供标题）")
        except Exception as e:
            self._json_error(400, f"请求格式错误：{e}")
            return

        prompt = (
            "你是资深游戏媒体编辑。只输出以下4行，不要任何其他文字：\n"
            "判断：适合/可参考/不适合（三选一）\n"
            "评分：数字1到10\n"
            "理由：一句话核心原因\n"
            "角度：写作角度一|写作角度二\n\n"
            f"标题：{title}"
        )

        req_data = json.dumps({
            "model": config.LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
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
