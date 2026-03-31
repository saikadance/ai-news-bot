"""
社交媒体热搜抓取模块：并发获取 B站游戏热门、微博游戏话题、抖音游戏话题。
任意平台失败不影响其他平台，失败时在 error 字段记录原因。

策略：
- B站：游戏区3日排行榜热门视频标题（直接反映玩家在看什么）
- 微博：从实时热搜中过滤游戏相关词条（取较大列表后关键词过滤）
- 抖音：从热搜列表中过滤游戏相关词条
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}
_TIMEOUT = 10  # 秒

# ── 游戏关键词列表（用于过滤微博/抖音热搜中的游戏相关条目）──────
_GAME_KEYWORDS: list[str] = [
    # 热门手游
    "原神", "崩坏", "绝区零", "星穹铁道", "鸣潮", "王者荣耀", "和平精英",
    "英雄联盟", "明日方舟", "蔚蓝档案", "少女前线", "阴阳师", "梦幻西游",
    "逆水寒", "剑网", "天涯明月刀", "黑神话", "悟空", "幻兽帕鲁",
    "蛋仔派对", "元梦之星", "金铲铲", "云顶之弈", "永劫无间", "暗区突围",
    "三角洲", "三国志", "率土之滨", "火影忍者", "龙族幻想", "神都夜行录",
    # 热门主机/PC游戏
    "塞尔达", "马里奥", "宝可梦", "最终幻想", "艾尔登法环", "荒野大镖客",
    "使命召唤", "刺客信条", "战神", "血源", "黑暗之魂", "艾尔登",
    "GTA", "我的世界", "Minecraft", "PUBG", "Apex", "CS", "瓦罗兰特", "守望先锋",
    "暗黑破坏神", "魔兽", "暴雪", "星际争霸", "炉石传说",
    "Fortnite", "堡垒之夜", "地平线", "巫师", "赛博朋克", "巫师",
    "死亡搁浅", "对马岛", "漫威蜘蛛侠", "战地", "FIFA", "EA FC",
    # 平台/品牌词
    "游戏", "手游", "端游", "网游", "主机游戏", "Steam", "Epic",
    "Xbox", "PlayStation", "PS5", "PS4", "Switch", "NS2",
    "电竞", "esports", "LOL", "LPL", "LCK", "S赛",
    # 游戏事件词
    "公测", "内测", "上线", "开服", "联动", "赛季", "版本更新", "DLC",
    "游戏展", "TGS", "E3", "Gamescom", "ChinaJoy", "CJ",
    # 游戏开发公司
    "米哈游", "腾讯游戏", "网易游戏", "莉莉丝", "完美世界",
    "卡普空", "Capcom", "万代南梦宫", "任天堂", "索尼互动",
]


@dataclass
class SocialHot:
    platform: str               # "B站游戏热门" / "微博游戏话题" / "抖音游戏话题"
    topics: list[str] = field(default_factory=list)   # 热搜词/视频标题列表
    fetched_at: str = ""        # 抓取时间
    error: str = ""             # 失败原因（成功则为空）


def _is_game_related(text: str) -> bool:
    """判断文本是否与游戏相关。"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in _GAME_KEYWORDS)


# ── B站游戏热门 ─────────────────────────────────────────────

def _fetch_bilibili() -> SocialHot:
    """通过 B站游戏区3日排行榜获取热门游戏内容标题。"""
    url = "https://api.bilibili.com/x/web-interface/ranking/region?rid=4&day=3&original=0"
    headers = {**_HEADERS, "Referer": "https://www.bilibili.com/"}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise ValueError(f"API code={data.get('code')}: {data.get('message')}")
        archives = data.get("data", [])
        topics = [
            a.get("title", "").strip()
            for a in archives
            if a.get("title") and len(a.get("title", "").strip()) > 2
        ][:10]
        if not topics:
            return SocialHot("B站游戏热门", error="返回数据为空")
        return SocialHot("B站游戏热门", topics=topics, fetched_at=_now())
    except Exception as e:
        # 回退：使用全站热搜但过滤游戏相关条目
        return _fetch_bilibili_fallback(str(e))


def _fetch_bilibili_fallback(reason: str) -> SocialHot:
    """B站游戏排行失败时，回退到全站热搜并过滤游戏相关条目。"""
    logger.debug("B站游戏排行失败（%s），尝试全站热搜过滤", reason)
    url = "https://api.bilibili.com/x/web-interface/search/square?limit=20"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        data = resp.json()
        all_items = [
            item.get("show_name", item.get("keyword", "")).strip()
            for item in data.get("data", {}).get("trending", {}).get("list", [])
        ]
        game_topics = [t for t in all_items if _is_game_related(t)][:10]
        if game_topics:
            return SocialHot("B站游戏热门", topics=game_topics, fetched_at=_now())
        return SocialHot("B站游戏热门", error=f"游戏区排行不可用（{reason[:60]}），全站热搜也无游戏相关")
    except Exception as e2:
        return SocialHot("B站游戏热门", error=f"{reason[:60]} | {str(e2)[:60]}")


# ── 微博游戏话题 ────────────────────────────────────────────

def _fetch_weibo() -> SocialHot:
    """从微博实时热搜中过滤出游戏相关话题（最多10条）。"""
    headers = {**_HEADERS, "Referer": "https://weibo.com/"}

    # 方案 A：side/hotSearch（realtime 字段，最多50条）
    url_a = "https://weibo.com/ajax/side/hotSearch"
    try:
        resp = requests.get(url_a, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            realtime = data.get("data", {}).get("realtime", [])
            all_words = [
                item.get("word", "").strip()
                for item in realtime
                if item.get("word") and len(item.get("word", "").strip()) > 1
            ]
            game_words = [w for w in all_words if _is_game_related(w)][:10]
            if game_words:
                return SocialHot("微博游戏话题", topics=game_words, fetched_at=_now())
            # 无游戏相关条目时，返回 top10 作为参考
            if all_words:
                return SocialHot(
                    "微博游戏话题",
                    topics=all_words[:10],
                    fetched_at=_now(),
                    error="今日暂无游戏相关热搜，显示全站前10"
                )
    except Exception as e:
        logger.debug("微博方案A失败：%s", e)

    # 方案 B：hot_band（band_list 字段）
    url_b = "https://weibo.com/ajax/statuses/hot_band"
    try:
        resp = requests.get(url_b, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            band_list = data.get("data", {}).get("band_list", [])
            all_words = [
                item.get("word", item.get("note", "")).strip()
                for item in band_list
                if (item.get("word") or item.get("note"))
                and len((item.get("word") or item.get("note", "")).strip()) > 1
            ]
            game_words = [w for w in all_words if _is_game_related(w)][:10]
            if game_words:
                return SocialHot("微博游戏话题", topics=game_words, fetched_at=_now())
            if all_words:
                return SocialHot(
                    "微博游戏话题",
                    topics=all_words[:10],
                    fetched_at=_now(),
                    error="今日暂无游戏相关热搜，显示全站前10"
                )
    except Exception as e:
        logger.debug("微博方案B失败：%s", e)

    return SocialHot("微博游戏话题", error="暂时无法获取微博热搜（接口受限）")


# ── 抖音游戏话题 ────────────────────────────────────────────

def _fetch_douyin() -> SocialHot:
    """从抖音热搜中过滤出游戏相关话题（最多10条）。"""
    url = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/?hot_search_type=0"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            word_list = data.get("word_list", [])
            all_words = [
                item.get("word", "").strip()
                for item in word_list
                if item.get("word") and len(item.get("word", "").strip()) > 1
            ]
            game_words = [w for w in all_words if _is_game_related(w)][:10]
            if game_words:
                return SocialHot("抖音游戏话题", topics=game_words, fetched_at=_now())
            if all_words:
                return SocialHot(
                    "抖音游戏话题",
                    topics=all_words[:10],
                    fetched_at=_now(),
                    error="今日暂无游戏相关热搜，显示全站前10"
                )
    except Exception as e:
        logger.debug("抖音方案A失败：%s", e)

    return SocialHot("抖音游戏话题", error="暂时无法获取抖音热搜（多个接口均失败）")


# ── 公共入口 ────────────────────────────────────────────────

def fetch_all_social() -> list[SocialHot]:
    """
    并发抓取三个平台游戏热门内容，返回 SocialHot 列表。
    任意平台失败不影响其他平台正常返回。
    """
    fetchers = {
        "B站游戏热门": _fetch_bilibili,
        "微博游戏话题": _fetch_weibo,
        "抖音游戏话题": _fetch_douyin,
    }
    results: dict[str, SocialHot] = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(fn): name for name, fn in fetchers.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as e:
                results[name] = SocialHot(name, error=str(e)[:120])

    ordered = [results[p] for p in ("B站游戏热门", "微博游戏话题", "抖音游戏话题") if p in results]
    for h in ordered:
        if h.error and not h.topics:
            logger.warning("[%s] 抓取失败：%s", h.platform, h.error)
        elif h.error:
            logger.info("[%s] 获取到 %d 条（%s）", h.platform, len(h.topics), h.error)
        else:
            logger.info("[%s] 抓取成功，获得 %d 条", h.platform, len(h.topics))
    return ordered


# ── 工具函数 ────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().strftime("%H:%M")


def cross_validate(
    social_hots: list[SocialHot],
    news_items: list,
) -> dict[str, list[tuple[str, str]]]:
    """
    交叉验证：检查每条热搜词是否有对应的新闻文章。
    返回 {热搜词: [(文章标题, 文章链接), ...]}。
    """
    results: dict[str, list[tuple[str, str]]] = {}
    for hot in social_hots:
        for topic in hot.topics:
            matched: list[tuple[str, str]] = []
            topic_kw = topic.lower().strip()
            if len(topic_kw) < 2:
                continue
            for item in news_items:
                title = item.text.split("\n")[0]
                if topic_kw in title.lower() or topic_kw in title:
                    matched.append((title[:80], item.permalink))
            if matched:
                results[topic] = matched
    return results
