from __future__ import annotations

import datetime
import html
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

import config
import content_fetcher
import topic_researcher

_GIST_ID = os.environ.get("GITHUB_GIST_ID", "")
_GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
_FAV_FILENAME = "favorites.json"
_NOTES_FILENAME = "notes.json"
_RESEARCH_FILENAME = "research_cache.json"
PORT = int(os.environ.get("PORT", "8765"))
HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_news.html")
ANALYSIS_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "interactive_analysis_cache.json",
)
RESEARCH_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "research_cache.json",
)
ANALYSIS_CACHE_VERSION = "v3"


def _gist_request(data: bytes | None = None, method: str = "GET") -> dict:
    if not _GIST_ID or not _GH_TOKEN:
        raise RuntimeError("Gist 未配置，缺少 GITHUB_GIST_ID 或 GITHUB_TOKEN。")

    req = urllib.request.Request(
        f"https://api.github.com/gists/{_GIST_ID}",
        data=data,
        method=method,
        headers={
            "Authorization": f"token {_GH_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "ai-news-bot",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gist_read_file(filename: str, default: dict) -> dict:
    if not _GIST_ID or not _GH_TOKEN:
        return default
    try:
        data = _gist_request()
        files = data.get("files", {})
        if filename not in files:
            return default
        content = files[filename].get("content", "") or ""
        return json.loads(content) if content else default
    except Exception:
        return default


def _gist_write_file(filename: str, payload_obj: dict) -> None:
    payload = json.dumps(
        {"files": {filename: {"content": json.dumps(payload_obj, ensure_ascii=False, indent=2)}}},
        ensure_ascii=False,
    ).encode("utf-8")
    _gist_request(payload, method="PATCH")


def _favorites_read() -> dict:
    return _gist_read_file(_FAV_FILENAME, {"items": []})


def _favorites_write(data: dict) -> None:
    _gist_write_file(_FAV_FILENAME, data)


def _notes_read() -> dict:
    return _gist_read_file(_NOTES_FILENAME, {"notes": {}})


def _notes_write(data: dict) -> None:
    _gist_write_file(_NOTES_FILENAME, data)


def _read_local_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_local_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _research_read() -> dict:
    default = {"items": {}}
    if _GIST_ID and _GH_TOKEN:
        data = _gist_read_file(_RESEARCH_FILENAME, default)
        return data if isinstance(data, dict) else default
    return _read_local_json(RESEARCH_CACHE_FILE, default)


def _research_write(data: dict) -> None:
    if _GIST_ID and _GH_TOKEN:
        _gist_write_file(_RESEARCH_FILENAME, data)
    else:
        _write_local_json(RESEARCH_CACHE_FILE, data)


def _read_analysis_cache() -> dict:
    if not os.path.exists(ANALYSIS_CACHE_FILE):
        return {}
    try:
        with open(ANALYSIS_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_analysis_cache(data: dict) -> None:
    try:
        with open(ANALYSIS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _cache_key(mode: str, title: str = "", link: str = "") -> str:
    return json.dumps(
        {
            "v": ANALYSIS_CACHE_VERSION,
            "mode": mode,
            "title": title.strip(),
            "link": link.strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _cache_get(mode: str, title: str = "", link: str = "") -> str:
    cache = _read_analysis_cache()
    return str(cache.get(_cache_key(mode, title, link), ""))


def _cache_set(mode: str, html_text: str, title: str = "", link: str = "") -> None:
    cache = _read_analysis_cache()
    cache[_cache_key(mode, title, link)] = html_text
    _write_analysis_cache(cache)


def _llm_chat(
    system_prompt: str,
    user_prompt: str,
    timeout: int = 60,
    max_tokens: int = 900,
    model: str | None = None,
) -> dict:
    req_model = model or config.LLM_FAST_MODEL or config.LLM_MODEL
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


def _log_analysis_debug(title: str, stage: str, payload: dict, is_complete: bool) -> None:
    try:
        print(
            "[analysis]",
            json.dumps(
                {
                    "title": title[:120],
                    "stage": stage,
                    "model": payload.get("model", ""),
                    "max_tokens": payload.get("max_tokens", 0),
                    "finish_reason": payload.get("finish_reason", ""),
                    "content_len": len(payload.get("content", "") or ""),
                    "complete": is_complete,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception:
        pass


def _normalize_llm_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    line = re.sub(r"^[\-\*\u2022]+\s*", "", line)
    line = re.sub(r"^\d+\.\s*", "", line)
    line = re.sub(r"^#+\s*", "", line)
    line = line.replace("\u200b", "")
    line = line.replace("**", "")
    line = line.replace("*", "")
    return line.strip()


def _parse_lines(content: str, mappings: list[tuple[str, str]]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    current_key: str | None = None
    for raw_line in content.splitlines():
        line = _normalize_llm_line(raw_line)
        if not line:
            continue
        matched = False
        for prefix, key in mappings:
            if line.startswith(prefix):
                current_key = key
                fields.setdefault(key, [])
                fields[key].append(line[len(prefix):].strip())
                matched = True
                break
        if not matched and current_key is not None:
            fields[current_key].append(line)
    return fields


def _join(fields: dict[str, list[str]], key: str) -> str:
    return " ".join(fields.get(key, [])).strip()


def _title_analysis_fields(content: str) -> dict[str, str | list[str]]:
    fields = _parse_lines(
        content,
        [
            ("判断：", "judgment"), ("判断:", "judgment"),
            ("评分：", "score"), ("评分:", "score"),
            ("价值分析：", "reason"), ("价值分析:", "reason"),
            ("理由：", "reason"), ("理由:", "reason"),
            ("角度一：", "a1"), ("角度一:", "a1"),
            ("角度二：", "a2"), ("角度二:", "a2"),
            ("角度三：", "a3"), ("角度三:", "a3"),
            ("建议标题：", "title"), ("建议标题:", "title"),
        ],
    )
    return {
        "judgment": _join(fields, "judgment"),
        "score": _join(fields, "score"),
        "reason": _join(fields, "reason"),
        "title": _join(fields, "title"),
        "angles": [_join(fields, key) for key in ("a1", "a2", "a3") if _join(fields, key)],
    }


def _looks_truncated_text(text: str) -> bool:
    text = text.strip()
    if not text:
        return True
    if len(text) < 6:
        return True
    bad_endings = (
        "，", "、", "：", ":", "；", ";", "（", "(", "[", "【", "“", "\"",
        "的", "了", "是", "及", "和", "与", "并", "并且", "以及", "但", "但其",
        "其", "为", "在", "对", "把", "将", "让", "使", "等", "例如",
    )
    return any(text.endswith(x) for x in bad_endings)


def _is_complete_title_analysis(content: str) -> bool:
    parsed = _title_analysis_fields(content)
    judgment = str(parsed["judgment"])
    score = str(parsed["score"])
    reason = str(parsed["reason"])
    suggest_title = str(parsed["title"])
    angles = parsed["angles"] if isinstance(parsed["angles"], list) else []
    if not judgment or not score or not reason or not suggest_title:
        return False
    if not re.search(r"\d+(\.\d+)?\s*/\s*10", score):
        return False
    if len(reason) < 30 or _looks_truncated_text(reason):
        return False
    if len(suggest_title) < 8 or _looks_truncated_text(suggest_title):
        return False
    if len(angles) < 3:
        return False
    for angle in angles[:3]:
        if len(angle) < 12 or _looks_truncated_text(angle):
            return False
    return True


def _generate_title_analysis_html(title: str) -> tuple[str, bool]:
    system_prompt = "你是一位资深游戏媒体编辑，请判断这条新闻是否值得深挖，并给出简洁的写作角度。"
    base_prompt = (
        f"请分析这条游戏新闻标题：{title}\n\n"
        "严格按下面格式输出：\n"
        "判断：适合/可参考/不适合\n"
        "评分：X/10\n"
        "价值分析：2-3句话\n"
        "角度一：...\n"
        "角度二：...\n"
        "角度三：...\n"
        "建议标题：..."
    )

    first_try = _llm_chat(system_prompt, base_prompt, timeout=20, max_tokens=900)
    first_content = str(first_try.get("content", "") or "")
    first_complete = _is_complete_title_analysis(first_content)
    _log_analysis_debug(title, "first_try", first_try, first_complete)
    if first_complete:
        return _parse_analyze_response(first_content), True

    retry_prompt = (
        f"请重新分析这条游戏新闻标题：{title}\n\n"
        "上一次输出不完整。请不要使用 Markdown，不要省略任何字段，每个字段单独占一行，必须完整输出以下 7 行：\n"
        "判断：适合/可参考/不适合\n"
        "评分：X/10\n"
        "价值分析：用2-3句话写完整\n"
        "角度一：...\n"
        "角度二：...\n"
        "角度三：...\n"
        "建议标题：..."
    )
    second_try = _llm_chat(
        system_prompt,
        retry_prompt,
        timeout=40,
        max_tokens=1400,
        model=config.LLM_MODEL or config.LLM_FAST_MODEL,
    )
    second_content = str(second_try.get("content", "") or "")
    second_complete = _is_complete_title_analysis(second_content)
    _log_analysis_debug(title, "retry", second_try, second_complete)
    if second_complete:
        return _parse_analyze_response(second_content), True
    return (
        '<div style="color:#e53935;font-size:12px;padding:6px 0;">'
        '本次 AI 分析返回不完整，已自动重试一次。请稍后再试。'
        '</div>',
        False,
    )


def _score_color(score: int) -> str:
    if score >= 8:
        return "#d93025"
    if score >= 6:
        return "#f29900"
    return "#888"


def _build_card_html(title: str, score_text: str, body: list[tuple[str, str]], angles: list[str], border: str) -> str:
    try:
        score_n = int(re.search(r"\d+", score_text).group()) if score_text else 0
    except Exception:
        score_n = 0
    score_html = ""
    if score_n:
        score_html = (
            f'<span style="background:{_score_color(score_n)};color:#fff;padding:2px 8px;'
            f'border-radius:10px;font-size:12px;">{score_n}/10</span>'
        )

    sections = []
    for label, content in body:
        if not content:
            continue
        sections.append(
            f'<p style="margin:6px 0 2px;font-size:12px;color:#555;font-weight:600;">{html.escape(label)}</p>'
            f'<p style="margin:0 0 6px;font-size:13px;line-height:1.6;">{html.escape(content)}</p>'
        )

    angles_html = ""
    if angles:
        items = "".join(f"<li style='margin-bottom:4px;'>{html.escape(a)}</li>" for a in angles if a)
        angles_html = (
            '<p style="margin:8px 0 2px;font-size:12px;color:#555;font-weight:600;">建议切入角度</p>'
            f'<ul style="margin:0 0 0 16px;font-size:13px;line-height:1.7;">{items}</ul>'
        )

    return (
        f'<div style="background:#fff;border-left:4px solid {border};padding:12px 14px;border-radius:4px;margin-top:4px;">'
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap;">'
        f'<strong style="font-size:14px;">{html.escape(title)}</strong>{score_html}'
        '</div>'
        + "".join(sections)
        + angles_html
        + '</div>'
    )


def _parse_analyze_response(content: str) -> str:
    fields = _parse_lines(
        content,
        [
            ("判断：", "judgment"), ("判断:", "judgment"),
            ("评分：", "score"), ("评分:", "score"),
            ("价值分析：", "reason"), ("价值分析:", "reason"),
            ("理由：", "reason"), ("理由:", "reason"),
            ("角度一：", "a1"), ("角度一:", "a1"),
            ("角度二：", "a2"), ("角度二:", "a2"),
            ("角度三：", "a3"), ("角度三:", "a3"),
            ("建议标题：", "title"), ("建议标题:", "title"),
        ],
    )
    judgment = _join(fields, "judgment")
    score = _join(fields, "score")
    reason = _join(fields, "reason")
    suggest_title = _join(fields, "title")
    angles = [_join(fields, key) for key in ("a1", "a2", "a3") if _join(fields, key)]

    if not judgment and not reason:
        safe = html.escape(content).replace("\n", "<br>")
        return f'<div style="font-size:13px;line-height:1.7;">{safe}</div>'

    body = [("判断", judgment), ("价值分析", reason), ("建议标题", suggest_title)]
    return _build_card_html("AI 深度分析", score, body, angles, "#1a73e8")


def _parse_full_response(content: str) -> str:
    fields = _parse_lines(
        content,
        [
            ("核心事件：", "event"), ("核心事件:", "event"),
            ("关键信息：", "data"), ("关键信息:", "data"),
            ("行业影响：", "impact"), ("行业影响:", "impact"),
            ("报道价值：", "score"), ("报道价值:", "score"),
            ("读者视角：", "reader"), ("读者视角:", "reader"),
            ("角度一：", "a1"), ("角度一:", "a1"),
            ("角度二：", "a2"), ("角度二:", "a2"),
            ("角度三：", "a3"), ("角度三:", "a3"),
        ],
    )
    body = [
        ("核心事件", _join(fields, "event")),
        ("关键信息", _join(fields, "data")),
        ("行业影响", _join(fields, "impact")),
        ("读者视角", _join(fields, "reader")),
    ]
    score = _join(fields, "score")
    angles = [_join(fields, key) for key in ("a1", "a2", "a3") if _join(fields, key)]

    if not any(content for _, content in body) and not angles:
        safe = html.escape(content).replace("\n", "<br>")
        return f'<div style="font-size:13px;line-height:1.7;">{safe}</div>'

    return _build_card_html("全文深度分析", score, body, angles, "#43a047")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html", "/latest_news.html"):
            self._serve_html()
        elif path == "/favorites":
            self._json_ok(_favorites_read())
        elif path == "/notes":
            self._handle_notes_get(parsed)
        elif path == "/research_topic":
            self._handle_research_get(parsed)
        else:
            self._json_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/analyze":
            self._handle_analyze()
        elif self.path == "/analyze_full":
            self._handle_analyze_full()
        elif self.path == "/favorites":
            self._handle_favorites()
        elif self.path == "/notes":
            self._handle_notes_post()
        elif self.path == "/research_topic":
            self._handle_research_post()
        else:
            self._json_error(404, "Not Found")

    def _serve_html(self):
        try:
            with open(HTML_FILE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._json_error(404, "报告文件不存在，请先运行 python scheduler.py --now")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _handle_analyze(self):
        try:
            body = self._read_json_body()
            title = body.get("title", "（未提供标题）")
            link = body.get("link", "")
            cached = _cache_get("title", title=title, link=link)
            if cached:
                self._json_ok({"html": cached, "cached": True})
                return
            html_text, is_complete = _generate_title_analysis_html(title)
            if is_complete:
                _cache_set("title", html_text, title=title, link=link)
            self._json_ok({"html": html_text, "cached": False, "complete": is_complete})
        except Exception as e:
            self._json_error(500, f"AI 分析失败：{e}")

    def _handle_analyze_full(self):
        try:
            body = self._read_json_body()
            link = body.get("link", "")
            title = body.get("title", "（未提供标题）")
            if not link:
                self._json_error(400, "缺少 link 字段")
                return

            cached = _cache_get("full", title=title, link=link)
            if cached:
                self._json_ok({"html": cached, "cached": True})
                return

            fetch_result = content_fetcher.fetch_article_text(link, timeout=20)
            article_content = str(fetch_result.get("text", "") or "")
            notice = str(fetch_result.get("notice", "") or "")
            if len(article_content) > 2500:
                article_content = article_content[:2500] + "\n\n[内容已截断]"

            if len(article_content.strip()) < 100:
                article_content = f"[仅标题] {title}"
                if not notice:
                    notice = "未能抓取到足够正文内容，以下分析仅基于标题。"

            system_prompt = (
                "你是一位资深游戏媒体主编，擅长从新闻全文中判断其报道价值、行业影响和可切入的写作角度。"
            )
            user_prompt = (
                f"请对以下游戏相关文章进行全文深度分析：\n\n标题：{title}\n\n正文：\n{article_content}\n\n"
                "请严格按如下格式输出，不要添加任何额外说明：\n"
                "核心事件：1-2句话概括文章核心内容\n"
                "关键信息：列出最重要的数据或事实；如果没有就写“无”\n"
                "行业影响：2-3句话说明影响\n"
                "报道价值：X/10，并用1-2句话说明原因\n"
                "读者视角：1-2句话说明读者最关心什么\n"
                "角度一：[标题式角度] - 用1-2句话说明如何切入\n"
                "角度二：[标题式角度] - 用1-2句话说明如何切入\n"
                "角度三：[标题式角度] - 用1-2句话说明如何切入"
            )
            llm_result = _llm_chat(
                system_prompt,
                user_prompt,
                timeout=35,
                max_tokens=900,
            )
            llm_text = str(llm_result.get("content", "") or "")
            html_card = _parse_full_response(llm_text)
            if notice:
                html_card = (
                    f'<div style="color:#f9a825;font-size:11px;padding:4px 0 2px;">提示：{html.escape(notice)}</div>'
                    + html_card
                )
            try:
                print(
                    "[full-analysis]",
                    json.dumps(
                        {
                            "title": title[:120],
                            "model": llm_result.get("model", ""),
                            "max_tokens": llm_result.get("max_tokens", 0),
                            "finish_reason": llm_result.get("finish_reason", ""),
                            "content_len": len(llm_text),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception:
                pass
            _cache_set("full", html_card, title=title, link=link)
            self._json_ok({"html": html_card})
        except Exception as e:
            self._json_error(500, f"全文分析失败：{e}")

    def _handle_favorites(self):
        try:
            body = self._read_json_body()
            link = body.get("link", "")
            if not link:
                self._json_error(400, "缺少 link 字段")
                return

            favorites = _favorites_read()
            items = favorites.get("items", [])
            idx = next((i for i, x in enumerate(items) if x.get("link") == link), None)
            if idx is not None:
                items.pop(idx)
                action = "removed"
            else:
                item = {
                    "title": body.get("title", ""),
                    "link": link,
                    "source": body.get("source", ""),
                    "analysis_html": body.get("analysis_html", ""),
                    "added_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                items.insert(0, item)
                action = "added"

            favorites["items"] = items
            warning = ""
            try:
                _favorites_write(favorites)
            except RuntimeError as e:
                warning = str(e)

            resp = {"action": action, "items": items}
            if warning:
                resp["warning"] = warning
            self._json_ok(resp)
        except Exception as e:
            self._json_error(500, f"收藏保存失败：{e}")

    def _handle_notes_get(self, parsed: urllib.parse.ParseResult):
        params = urllib.parse.parse_qs(parsed.query)
        link = params.get("link", [""])[0]
        if not link:
            self._json_error(400, "缺少 link 参数")
            return
        notes = _notes_read().get("notes", {}).get(link, [])
        self._json_ok({"notes": notes})

    def _handle_notes_post(self):
        try:
            body = self._read_json_body()
            link = body.get("link", "")
            text = (body.get("text", "") or "").strip()
            note_id = body.get("note_id", "")
            if not link:
                self._json_error(400, "缺少 link 字段")
                return
            if not text and not note_id:
                self._json_error(400, "缺少 text 或 note_id")
                return

            notes_data = _notes_read()
            notes_map = notes_data.setdefault("notes", {})
            link_notes = list(notes_map.get(link, []))
            if note_id:
                link_notes = [n for n in link_notes if n.get("id") != note_id]
            else:
                link_notes.append(
                    {
                        "id": str(uuid.uuid4())[:8],
                        "text": text,
                        "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
            notes_map[link] = link_notes

            warning = ""
            try:
                _notes_write(notes_data)
            except RuntimeError as e:
                warning = str(e)

            resp = {"notes": link_notes}
            if warning:
                resp["warning"] = warning
            self._json_ok(resp)
        except Exception as e:
            self._json_error(500, f"便利贴保存失败：{e}")

    def _handle_research_get(self, parsed: urllib.parse.ParseResult):
        params = urllib.parse.parse_qs(parsed.query)
        link = params.get("link", [""])[0]
        if not link:
            self._json_error(400, "缺少 link 参数")
            return
        cache = _research_read()
        item = cache.get("items", {}).get(link)
        self._json_ok({"item": item, "cached": bool(item)})

    def _handle_research_post(self):
        try:
            body = self._read_json_body()
            title = (body.get("title", "") or "").strip()
            link = (body.get("link", "") or "").strip()
            source = (body.get("source", "") or "").strip()
            force = bool(body.get("force", False))
            if not title or not link:
                self._json_error(400, "缺少 title 或 link 字段")
                return

            cache = _research_read()
            items = cache.setdefault("items", {})
            cached_item = items.get(link)
            if cached_item and not force:
                self._json_ok({"item": cached_item, "cached": True})
                return

            item = topic_researcher.generate_research_package(title=title, link=link, source=source)
            items[link] = item

            warning = ""
            try:
                _research_write(cache)
            except RuntimeError as e:
                warning = str(e)

            resp = {"item": item, "cached": False}
            if warning:
                resp["warning"] = warning
            self._json_ok(resp)
        except Exception as e:
            self._json_error(500, f"资料研究失败：{e}")

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
    host = os.environ.get("HOST", "localhost")
    bind_host = "0.0.0.0" if host != "localhost" else "localhost"
    url = f"http://{host}:{PORT}"
    server = ThreadingHTTPServer((bind_host, PORT), Handler)
    print(f"本地服务器已启动：{url}")
    if open_browser and host == "localhost":
        Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    start(open_browser=True)
