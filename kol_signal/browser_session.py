from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


MODULE_DIR = Path(__file__).resolve().parent
STATE_ROOT = MODULE_DIR / "browser_state"

LOGIN_URLS = {
    "weibo": "https://m.weibo.cn/",
    "bilibili": "https://www.bilibili.com/",
}


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "当前未安装 playwright。请先执行 `pip install playwright`，再执行 "
            "`python -m playwright install chromium`。"
        ) from e
    return sync_playwright


def session_dir(platform: str) -> Path:
    path = STATE_ROOT / platform.strip().lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_state_path(platform: str) -> Path:
    return session_dir(platform) / "storage_state.json"


def has_saved_session(platform: str) -> bool:
    state = storage_state_path(platform)
    return state.exists() and state.stat().st_size > 0


@contextmanager
def open_persistent_context(platform: str, headless: bool = False) -> Iterator[tuple[object, object]]:
    sync_playwright = _import_playwright()
    user_data_dir = session_dir(platform)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(user_data_dir),
            headless=headless,
            viewport={"width": 1440, "height": 900},
            accept_downloads=False,
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page
        finally:
            try:
                context.storage_state(path=str(storage_state_path(platform)))
            except Exception:
                pass
            context.close()


def interactive_login(platform: str, start_url: str | None = None, wait_seconds: int = 180) -> dict:
    platform_name = platform.strip().lower()
    target_url = start_url or LOGIN_URLS.get(platform_name, "about:blank")
    with open_persistent_context(platform_name, headless=False) as (context, page):
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        print(f"[kol_signal] 已打开 {platform_name} 登录页：{target_url}")
        print("[kol_signal] 请在弹出的浏览器里完成扫码或账号登录。")
        try:
            input("[kol_signal] 登录完成后，回到终端按回车保存本地会话...")
        except EOFError:
            print(f"[kol_signal] 当前终端不可交互，将自动等待 {wait_seconds} 秒后保存会话。")
            page.wait_for_timeout(wait_seconds * 1000)
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []
    state_path = storage_state_path(platform_name)
    return {
        "platform": platform_name,
        "saved": state_path.exists(),
        "storage_state": str(state_path),
        "cookies_count": len(cookies),
    }


def load_storage_state(platform: str) -> dict:
    path = storage_state_path(platform)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
