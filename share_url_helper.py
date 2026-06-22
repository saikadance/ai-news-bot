from __future__ import annotations

import configparser
import os
import re
from pathlib import Path

DEFAULT_PUBLIC_REPORT_URL = "https://saikadance.github.io/ai-news-bot/"


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
        return DEFAULT_PUBLIC_REPORT_URL

    owner, repo = repo_full_name.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if not owner or not repo:
        return DEFAULT_PUBLIC_REPORT_URL

    if repo.lower() == f"{owner.lower()}.github.io":
        return f"https://{owner}.github.io/"
    return f"https://{owner}.github.io/{repo}/"


def resolve_public_report_url(url: str = "") -> str:
    """
    Normalize any candidate report URL to the preferred public entry.

    Priority:
    1. Explicit REPORT_PUBLIC_URL / GITHUB_PAGES_URL
    2. A valid http(s) input URL when no public entry is configured
    3. Best-effort inferred GitHub Pages URL
    """
    public_url = guess_pages_url()
    if public_url:
        return public_url

    clean = (url or "").strip()
    if re.match(r"^https?://", clean, re.I):
        return clean
    return DEFAULT_PUBLIC_REPORT_URL


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
