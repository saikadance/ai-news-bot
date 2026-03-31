"""
GitHub Gist 自动上传模块。
第一次运行时自动创建 Gist，后续每次更新同一个 Gist，返回可预览的固定 URL。
若未配置 GITHUB_TOKEN，返回空字符串（静默跳过）。
"""
from __future__ import annotations

import logging
import os
import re

import requests

import config

logger = logging.getLogger(__name__)

GIST_API = "https://api.github.com/gists"
FILENAME = "latest_news.html"


def upload(html_content: str) -> str:
    """
    上传 HTML 内容到 GitHub Gist，返回可直接预览的 URL。
    第一次调用自动创建 Gist 并将 ID 写回 .env；后续调用更新同一个 Gist。
    未配置 Token 时返回空字符串。
    """
    token = config.GITHUB_TOKEN
    if not token:
        logger.debug("未配置 GITHUB_TOKEN，跳过 Gist 上传")
        return ""

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
            # 更新已有 Gist
            resp = requests.patch(
                f"{GIST_API}/{gist_id}", json=payload, headers=headers, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("Gist 已更新：%s", data.get("html_url", ""))
        else:
            # 第一次：创建新 Gist
            resp = requests.post(GIST_API, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            gist_id = data["id"]
            logger.info("Gist 已创建：%s", data.get("html_url", ""))
            # 将新 ID 写回 .env，方便下次使用
            _save_gist_id_to_env(gist_id)

        # 返回 htmlpreview 可直接渲染 HTML 的链接
        raw_url = data["files"][FILENAME]["raw_url"]
        # raw_url 每次更新会变化（包含 commit hash），用稳定的 gist raw 地址替代
        preview_url = (
            f"https://htmlpreview.github.io/?"
            f"https://gist.github.com/raw/{gist_id}/{FILENAME}"
        )
        return preview_url

    except requests.RequestException as e:
        logger.error("Gist 上传失败：%s", e)
        return ""


def _save_gist_id_to_env(gist_id: str) -> None:
    """将新生成的 Gist ID 写入 .env 文件的 GITHUB_GIST_ID 行。"""
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

        # 同时更新内存中的值，避免本次运行还用旧值
        config.GITHUB_GIST_ID = gist_id
        logger.info("Gist ID 已自动写入 .env：%s", gist_id)
    except OSError as e:
        logger.warning("无法写回 .env，请手动填写 GITHUB_GIST_ID=%s（%s）", gist_id, e)
