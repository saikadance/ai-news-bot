"""
测试 config.RSS_FEEDS 中所有源的可用性，输出报告到控制台和 feed_test_result.txt。
运行方式：python test_feeds.py
"""
from __future__ import annotations

import socket
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

# 强制 stdout 使用 UTF-8，避免 Windows GBK 编码报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import feedparser

import config

TIMEOUT = 15


def source_name(url: str) -> str:
    try:
        host = urlparse(url).netloc.replace("www.", "").replace("feeds.", "")
        return host.split(".")[0]
    except Exception:
        return url[:30]


URL_TO_NAME = {
    "feedburner.com/ign":       "IGN (英文)",
    "feedburner.com/Kotaku":    "Kotaku",
    "gamespot.com":             "GameSpot",
    "rockpapershotgun.com":     "Rock Paper Shotgun",
    "pcgamer.com":              "PC Gamer",
    "eurogamer.net":            "Eurogamer",
    "polygon.com":              "Polygon",
    "feedx.net":                "3DM (feedx)",
    "nadianshi.com":            "手游那点事",
    "yystv.cn":                 "游研社",
    "4gamer.net":               "4Gamer (日文)",
}


def get_name(url: str) -> str:
    for key, name in URL_TO_NAME.items():
        if key in url:
            return name
    return source_name(url)


def test_feed(url: str) -> dict:
    name = get_name(url)
    result = {
        "name": name,
        "url": url,
        "status": "失败",
        "count": 0,
        "latest": "-",
        "error": "",
    }

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(TIMEOUT)
    try:
        feed = feedparser.parse(url)

        if feed.bozo and not feed.entries:
            result["error"] = f"解析错误：{type(feed.bozo_exception).__name__}: {feed.bozo_exception}"
            return result

        entries = feed.entries
        result["count"] = len(entries)

        if entries:
            result["status"] = "成功"
            # 找最新文章时间
            for entry in entries[:5]:
                for field in ("published_parsed", "updated_parsed"):
                    t = entry.get(field)
                    if t:
                        try:
                            dt = datetime(*t[:6], tzinfo=timezone.utc)
                            result["latest"] = dt.strftime("%Y-%m-%d %H:%M")
                            break
                        except Exception:
                            pass
                if result["latest"] != "-":
                    break
            if result["latest"] == "-":
                result["latest"] = "时间未知"
        else:
            result["status"] = "空（无条目）"

    except Exception as e:
        result["error"] = str(e)
    finally:
        socket.setdefaulttimeout(old_timeout)

    return result


def main() -> None:
    print(f"\n{'=' * 70}")
    print(f"  游戏新闻 RSS 源可用性测试  ·  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'=' * 70}")
    print(f"{'来源':<22} {'状态':<8} {'条目数':<8} {'最新文章时间':<18} {'备注'}")
    print(f"{'-' * 70}")

    results = []

    for url in config.RSS_FEEDS:
        results.append(test_feed(url))

    lines = []
    success = 0
    fail = 0
    for r in results:
        status_icon = "[OK]" if r["status"] == "成功" else "[!!]"
        note = r["error"][:35] if r["error"] else ""
        line = f"{r['name']:<22} {status_icon} {r['status']:<6} {r['count']:<8} {r['latest']:<18} {note}"
        print(line)
        lines.append(line)
        if r["status"] == "成功":
            success += 1
        else:
            fail += 1

    summary = f"\n结果：{success} 个成功，{fail} 个失败"
    print(f"{'-' * 70}")
    print(summary)
    print(f"{'=' * 70}\n")

    # 写入文件（UTF-8，支持中文）
    report_path = r"e:\AI 选题关注\feed_test_result.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"游戏新闻 RSS 源可用性测试报告\n")
        f.write(f"测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 70 + "\n")
        f.write(f"{'来源':<22} {'状态':<8} {'条目数':<8} {'最新文章时间':<18} {'备注'}\n")
        f.write("-" * 70 + "\n")
        for line in lines:
            f.write(line + "\n")
        f.write("-" * 70 + "\n")
        f.write(summary + "\n")
        f.write("\n失败源详情：\n")
        for r in results:
            if r["status"] != "成功":
                f.write(f"  [{r['name']}] {r['url']}\n")
                f.write(f"    原因：{r['error'] or r['status']}\n")

    print(f"报告已保存至：{report_path}")


if __name__ == "__main__":
    main()
