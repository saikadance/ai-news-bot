import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ───────────────────────────────────────────────
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini-2.0-flash")
# 批量文章筛选用的轻量模型（无深度推理）；未配置时回退到 LLM_MODEL
LLM_FAST_MODEL: str = os.getenv("LLM_FAST_MODEL", "") or LLM_MODEL

# ── 热点聚类参数 ───────────────────────────────────────────
# 一个话题最少需要几家不同媒体报道才算热点（默认2）
CLUSTER_MIN_SOURCES: int = int(os.getenv("CLUSTER_MIN_SOURCES", "2"))
# 最多展示几个热点聚焦（默认5）
CLUSTER_MAX_COUNT: int = int(os.getenv("CLUSTER_MAX_COUNT", "5"))

# ── 飞书 ───────────────────────────────────────────────
FEISHU_WEBHOOK_URL: str = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_WEBHOOK_SECRET: str = os.getenv("FEISHU_WEBHOOK_SECRET", "")

# ── 运行参数 ────────────────────────────────────────────
SCHEDULE_TIME: str = os.getenv("SCHEDULE_TIME", "10:00")
LOOKBACK_HOURS: int = int(os.getenv("LOOKBACK_HOURS", "24"))
TOP_N: int = int(os.getenv("TOP_N", "5"))

# ── 共享配置 ────────────────────────────────────────────
# SHARE_MODE: "gist" | "feishu_msg" | ""
SHARE_MODE: str = os.getenv("SHARE_MODE", "feishu_msg")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_GIST_ID: str = os.getenv("GITHUB_GIST_ID", "")

# ── RSS 新闻源 ─────────────────────────────────────────
# 英文媒体（国际热点）
_EN_FEEDS = [
    "http://feeds.feedburner.com/ign/news",          # IGN
    "https://www.gamespot.com/feeds/mashup/",        # GameSpot
    "https://feeds.feedburner.com/Kotaku",           # Kotaku（feedburner 备用地址）
    "https://www.rockpapershotgun.com/feed",         # Rock Paper Shotgun
    "https://www.pcgamer.com/rss/",                  # PC Gamer
    "https://www.eurogamer.net/?format=rss",         # Eurogamer
    "https://www.polygon.com/rss/gaming/index.xml",  # Polygon
]

# 中文媒体（国内热点）
_CN_FEEDS = [
    "https://feedx.net/rss/3dmgame.xml",    # 3DM（feedx 直连，稳定）
    "http://www.nadianshi.com/feed",         # 手游那点事
    "https://www.yystv.cn/rss/feed",         # 游研社
    "https://www.ithome.com/rss/",           # IT之家（全站，自动过滤游戏相关内容）
]

# 日文媒体
_JP_FEEDS = [
    "https://www.4gamer.net/rss/index.xml",  # 4Gamer
]

# 合并后的完整 RSS 源列表（可在此增删）
RSS_FEEDS: list[str] = _EN_FEEDS + _CN_FEEDS + _JP_FEEDS

# 需要按游戏关键词过滤的 RSS 源（全站内容但非游戏专属媒体）
GAME_FILTER_FEEDS: set[str] = {
    "https://www.ithome.com/rss/",
}

# 游民星空 HTML 抓取地址（无 RSS，直接抓新闻列表页）
GAMERSKY_URL: str = "https://www.gamersky.com/news/"


def validate():
    """启动时检查必填配置，缺失则报错提示。"""
    missing = []
    if not LLM_API_KEY or "请填写" in LLM_API_KEY:
        missing.append("LLM_API_KEY")
    if not FEISHU_WEBHOOK_URL or "请填写" in FEISHU_WEBHOOK_URL:
        missing.append("FEISHU_WEBHOOK_URL")
    if missing:
        raise EnvironmentError(
            f"请在 .env 文件中填写以下必填配置：{', '.join(missing)}"
        )
