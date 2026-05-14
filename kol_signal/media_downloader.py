from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import requests

from models import KOLPost
from storage import MEDIA_DIR, MODULE_DIR


def _guess_suffix(url: str, content_type: str = "") -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    guessed = mimetypes.guess_extension(content_type.split(";")[0].strip()) if content_type else None
    return guessed or ".jpg"


def download_post_media(posts: list[KOLPost], timeout: int = 20) -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
    )

    for post in posts:
        post.downloaded_media_paths = []
        post.embedded_media_data_urls = []
        for idx, url in enumerate(post.media_urls[:4], start=1):
            try:
                resp = session.get(
                    url,
                    timeout=timeout,
                    headers={
                        "Referer": post.url or "https://m.weibo.cn/",
                    },
                )
                resp.raise_for_status()
                if not resp.content:
                    continue
                suffix = _guess_suffix(url, resp.headers.get("Content-Type", ""))
                filename = f"{post.platform}_{post.account_id}_{post.post_id}_{idx}{suffix}"
                path = MEDIA_DIR / filename
                path.write_bytes(resp.content)
                post.downloaded_media_paths.append(str(path.relative_to(MODULE_DIR.parent)))
                mime = resp.headers.get("Content-Type", "").split(";")[0].strip() or (
                    mimetypes.guess_type(path.name)[0] or "image/jpeg"
                )
                encoded = base64.b64encode(resp.content).decode("ascii")
                post.embedded_media_data_urls.append(f"data:{mime};base64,{encoded}")
            except Exception:
                continue
