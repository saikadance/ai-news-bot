from __future__ import annotations

import configparser
import os
import re
from pathlib import Path


def guess_pages_url() -> str:
    """Best-effort guess for the public GitHub Pages URL."""
    explicit = (
        os.environ.get("REPORT_PUBLIC_URL", "").strip()
        or os.environ.get("GITHUB_PAGES_URL", "").strip()
    )
    if explicit:
        return _normalize_public_url(explicit)

    repo_full_name = _guess_repo_full_name()
    if not repo_full_name or "/" not in repo_full_name:
        return ""

    owner, repo = repo_full_name.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return ""

    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def _normalize_public_url(url: str) -> str:
    clean = url.strip()
    if not clean:
        return ""
    if not re.match(r"^https?://", clean, re.I):
        clean = "https://" + clean.lstrip("/")
    return clean.rstrip("/") + "/"


def _guess_repo_full_name() -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo and "/" in env_repo:
        return env_repo

    git_config_path = Path(__file__).resolve().parent / ".git" / "config"
    if not git_config_path.exists():
        return ""

    parser = configparser.ConfigParser()
    try:
        parser.read(git_config_path, encoding="utf-8")
    except Exception:
        return ""

    if not parser.has_section('remote "origin"'):
        return ""

    remote_url = parser.get('remote "origin"', "url", fallback="").strip()
    return _extract_repo_from_remote(remote_url)


def _extract_repo_from_remote(remote_url: str) -> str:
    if not remote_url:
        return ""

    patterns = [
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, remote_url, re.I)
        if match:
            owner = match.group("owner").strip()
            repo = match.group("repo").strip()
            if owner and repo:
                return f"{owner}/{repo}"
    return ""
