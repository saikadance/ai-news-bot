import os
from dotenv import load_dotenv

load_dotenv()

# ── LLM ───────────────────────────────────────────────
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-5.6-luna")
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
SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

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


# ── 选题评分标准（统一 rubric）──────────────────────────────
# 四个评分入口共用同一套维度与档位，避免各写各的导致口径不一致。
# 被 llm_analyzer.py（Top5/单篇）与 server.py（页面 AI 分析/全文分析）引用。

EDITOR_PERSONA: str = (
    "你是一位拥有10年经验的资深游戏媒体编辑，擅长判断哪些游戏新闻最值得深度报道。"
)

SCORING_RUBRIC: str = """\
【判断维度】
1. 话题热度 —— 是否会引发玩家广泛讨论？有无破圈潜力？
2. 内容深度 —— 有没有可深挖的角度（行业影响、商业逻辑、玩家体验、开发内幕等）？
3. 时效性 —— 新鲜程度，是否是当下热点？
4. 受众共鸣 —— 是否触及玩家痛点、期待或情绪？
5. 选题适配度 —— 是否适合本媒体调性？有无原创写作空间、能否成稿？

【评分标准（1-10分，请严格遵守分布）】
- 1-2分：广告/软文/水稿，或与游戏无关、纯搬运，无选题价值，不予收录
- 3-4分：版本更新/活动通知/小体量资讯，仅对垂直圈层用户有参考价值，无法出圈
- 5-6分：有一定讨论度的行业动态，但深度或受众有限，可作为配稿参考
- 7-8分：话题热度或内容深度明显突出，适合大多数玩家读者，值得写稿
- 9分：多个维度同时突出、极易引发广泛讨论的重大事件，需极其严格
- 10分：现象级破圈事件，全网级关注，当天顶流（极罕见，几乎不给）
大多数新闻应落在 5-7 分区间，打 8 分以上需要真正有过人之处。"""


SCORING_METHOD: str = """\
【判断方法（按顺序逐条核对，不要凭感觉打分）】

第一步 · 先排除：命中以下任一 → 直接 1-2 分或淘汰
- 广告 / 软文 / 带货 / 抽奖活动
- 与游戏无关
- 纯转载、无任何新信息

第二步 · 逐维度核对信号并计分：

1. 话题热度（0-2 分）—— 看传播潜力
   - 涉及知名 IP / 头部厂商 / 明星制作人（任天堂、索尼、R星、米哈游、宫崎英高等）
   - 多个不同媒体同时报道同一事件（非单一来源）
   - 含争议 / 冲突 / 爆料 / 反转等传播性元素
   命中 ≥2 个 → 2 分；命中 1 个 → 1 分；0 个 → 0 分

2. 内容深度（0-2 分）—— 看能挖多深
   - 结构性事件（收购 / 裁员 / 财报 / 战略转型 / 政策）
   - 独家信息 / 开发内幕 / 一手采访 / 数据
   - 触及行业趋势或模式变革（非单一产品消息）
   命中 ≥2 个 → 2 分；命中 1 个 → 1 分；0 个 → 0 分

3. 时效性（0-1 分）—— 看新鲜度
   - 24 小时内新事件 / 首次披露 / 重大新进展 → 1 分
   - 纯回顾 / 旧闻重提 → 0 分

4. 受众共鸣（0-2 分）—— 看情绪触点
   - 触及玩家核心利益（价格 / 权益 / 内容删减 / 服务）
   - 引发"玩家 vs 厂商"或"玩家 vs 玩家"情绪对立
   - 有情怀 / 期待元素（经典 IP 复活、续作官宣等）
   命中 ≥2 个 → 2 分；命中 1 个 → 1 分；0 个 → 0 分

5. 选题适配度（0-3 分）—— 看能否成稿
   - 本媒体擅长 / 目标读者关心（游戏 / ACG / 科技圈层）
   - 有足够素材展开成独立文章（非一句话资讯）
   - 能形成独特观点 / 切入角度（非复述新闻）
   命中 3 个 → 3 分；命中 2 个 → 2 分；命中 1 个 → 1 分；0 个 → 0 分

第三步 · 加减分修正
- 加分（+1，可突破维度上限）：独家首发 / 一手信源；涉及多个头部厂商的连锁反应；有明确数据 / 财报 / 官方声明佐证
- 扣分：事件已过气（>72 小时且无新进展）−2；标题党但内容空 −2；单一来源且无佐证 −1

第四步 · 汇总
总分 = 五个维度分之和 + 加分 − 扣分，最终落在 1-10 分。
写理由时必须逐条引用命中的信号，说明"为什么给这个分"。"""
