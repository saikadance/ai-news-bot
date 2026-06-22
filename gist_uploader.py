"""
GitHub Gist upload helper.

The first successful run creates a secret gist and stores its ID in `.env`.
Later runs update the same gist and return a preview URL for the latest HTML.
"""
from __future__ import annotations

import logging
import os
import re

import requests

import config
import share_url_helper

logger = logging.getLogger(__name__)

GIST_API = "https://api.github.com/gists"
FILENAME = "latest_news.html"


def upload(html_content: str) -> str:
    """Upload HTML content to GitHub Gist and return a browser-friendly URL."""
    public_url = share_url_helper.resolve_public_report_url()
    token = config.GITHUB_TOKEN
    if not token:
        logger.debug("GITHUB_TOKEN not configured, skip Gist upload")
        return public_url

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "description": "游戏选题日报 - 自动更新",
        "public": False,
        "files": {FILENAME: {"content": html_content}},
    }

    gist_id = config.GITHUB_GIST_ID

    try:
        if gist_id:
            resp = requests.patch(
                f"{GIST_API}/{gist_id}", json=payload, headers=headers, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Gist 已更新：%s", data.get("html_url", ""))
        else:
            resp = requests.post(GIST_API, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            gist_id = data["id"]
            logger.info("Gist 已创建：%s", data.get("html_url", ""))
            _save_gist_id_to_env(gist_id)

        # Prefer the public Pages URL as the outward-facing entry. Gist remains
        # useful for state storage and as a fallback only.
        raw_url = data["files"][FILENAME]["raw_url"]
        return public_url or f"https://htmlpreview.github.io/?{raw_url}"

    except requests.RequestException as e:
        logger.error("Gist 上传失败：%s", e)
        return public_url


def _save_gist_id_to_env(gist_id: str) -> None:
    """Persist the generated Gist ID back into `.env`."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            content = f.read()

        if "GITHUB_GIST_ID=" in content:
            content = re.sub(
                r"GITHUB_GIST_ID=.*", f"GITHUB_GIST_ID={gist_id}", content
            )
        else:
            content += f"\nGITHUB_GIST_ID={gist_id}\n"

        with open(env_path, "w", encoding="utf-8") as f:
            f.write(content)

        config.GITHUB_GIST_ID = gist_id
        logger.info("Gist ID 已自动写入 .env：%s", gist_id)
    except OSError as e:
        logger.warning("无法写回 .env，请手动填写 GITHUB_GIST_ID=%s：%s", gist_id, e)
