"""
Vercel Serverless Function: POST /api/analyze
替代本地 server.py 的 /analyze 接口，在云端安全调用 LLM。
"""
from __future__ import annotations
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler

LLM_KEY = os.environ.get("LLM_API_KEY", "")
LLM_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")


def _parse_response(content: str) -> dict:
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
                    score = "".join(c for c in val if c.isdigit())[:2]
                elif key == "r":
                    reason = val
                elif key == "a":
                    for sep in ("|", "｜", "/"):
                        if sep in val:
                            angles = [v.strip() for v in val.split(sep)]
                            break
                    else:
                        angles = [val]
                break
    return {"judgment": judgment, "score": score, "reason": reason, "angles": angles}


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            title = body.get("title", "")
        except Exception as e:
            return self._error(400, f"请求格式错误：{e}")

        if not LLM_KEY:
            return self._error(500, "LLM_API_KEY 未配置")

        prompt = (
            "你是资深游戏媒体编辑。只输出以下4行，不要任何其他文字：\n"
            "判断：适合/可参考/不适合（三选一）\n"
            "评分：数字1到10\n"
            "理由：一句话核心原因\n"
            "角度：写作角度一|写作角度二\n\n"
            f"标题：{title}"
        )

        req_data = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
        }).encode("utf-8")

        req = urllib.request.Request(
            LLM_URL.rstrip("/") + "/chat/completions",
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_KEY}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            llm_text = result["choices"][0]["message"]["content"]
            parsed = _parse_response(llm_text)
            self._ok({"result": parsed, "raw": llm_text})
        except Exception as e:
            self._error(500, f"LLM 调用失败：{e}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _ok(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)
