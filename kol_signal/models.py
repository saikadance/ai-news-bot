from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class KOLAccount:
    platform: str
    account_id: str
    display_name: str
    homepage: str = ""
    priority: int = 1
    tags: list[str] = field(default_factory=list)
    focus_keywords: list[str] = field(default_factory=list)
    require_image_review: bool = False
    enabled: bool = True


@dataclass
class KOLMetrics:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    favorites: int = 0

    def normalized_score(self, account_priority: int = 1) -> float:
        base = (
            self.views * 0.002
            + self.likes * 1.2
            + self.comments * 1.6
            + self.shares * 2.2
            + self.favorites * 1.4
        )
        return round(base * max(account_priority, 1), 2)


@dataclass
class KOLPost:
    platform: str
    account_id: str
    account_name: str
    post_id: str
    url: str
    title: str
    text: str
    published_at: str
    metrics: KOLMetrics = field(default_factory=KOLMetrics)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    extracted_keywords: list[str] = field(default_factory=list)
    media_urls: list[str] = field(default_factory=list)
    downloaded_media_paths: list[str] = field(default_factory=list)
    embedded_media_data_urls: list[str] = field(default_factory=list)
    needs_image_review: bool = False

    def normalized_score(self, account_priority: int = 1) -> float:
        return self.metrics.normalized_score(account_priority=account_priority)


@dataclass
class MatchedNewsItem:
    title: str
    link: str
    source: str
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class SignalCandidate:
    event_title: str
    summary: str
    platforms: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    matched_news: list[MatchedNewsItem] = field(default_factory=list)
    related_posts: list[KOLPost] = field(default_factory=list)
    impact_score: float = 0.0
    recommendation: str = "observe"


@dataclass
class SignalReport:
    generated_at: str
    accounts_count: int
    posts_count: int
    candidates: list[SignalCandidate] = field(default_factory=list)
    posts_preview: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, notes: list[str] | None = None) -> "SignalReport":
        return cls(
            generated_at=datetime.utcnow().isoformat() + "Z",
            accounts_count=0,
            posts_count=0,
            candidates=[],
            posts_preview=[],
            notes=notes or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
