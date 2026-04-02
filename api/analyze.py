import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler


def _cors(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


class handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        _cors(self)
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            title = body.get("title", "")
        except Exception as e:
            return self._err(400, "bad request: " + str(e))

        key   = os.environ.get("LLM_API_KEY", "")
        base  = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        model = os.environ.get("LLM_MODEL", "deepseek-chat")

        if not key:
            return self._err(500, "LLM_API_KEY not configured")

        prompt = (
            "你是资深游戏媒体编辑。只输出以下4行，不要任何其他文字：\n"
            "判断：适合/可参考/不适合（三选一）\n"
            "评分：数字1到10\n"
            "理由：一句话核心原因\n"
            "角度：写作角度一|写作角度二\n\n"
            "标题：" + title
        )

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
        }).encode("utf-8")

        req = urllib.request.Request(
            base + "/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            result = _parse(text)
            self._ok({"result": result, "raw": text})
        except Exception as e:
            self._err(500, "LLM error: " + str(e))

    def _ok(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        _cors(self)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        _cors(self)
        self.end_headers()
        self.wfile.write(body)


def _parse(text):
    j = s = r = ""
    angles = []
    for line in text.splitlines():
        line = line.strip()
        for prefix, key in [("判断：","j"),("判断:","j"),("评分：","s"),("评分:","s"),
                             ("理由：","r"),("理由:","r"),("角度：","a"),("角度:","a")]:
            if line.startswith(prefix):
                val = line[len(prefix):].strip()
                if key == "j": j = val
                elif key == "s": s = "".join(c for c in val if c.isdigit())[:2]
                elif key == "r": r = val
                elif key == "a":
                    for sep in ("|","｜","/"):
                        if sep in val:
                            angles = [v.strip() for v in val.split(sep)]
                            break
                    else:
                        angles = [val]
                break
    return {"judgment": j, "score": s, "reason": r, "angles": angles}
