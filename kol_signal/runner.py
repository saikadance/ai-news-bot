from __future__ import annotations

import argparse
from pathlib import Path

from analyzer import build_signal_report
from fetchers import get_fetcher
from media_downloader import download_post_media
from storage import DEFAULT_CONFIG, DEFAULT_REPORT, load_accounts, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="KOL 信号抓取与交叉验证独立入口")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="账号配置文件路径")
    parser.add_argument("--output", default=str(DEFAULT_REPORT), help="输出报告路径")
    parser.add_argument("--limit", type=int, default=8, help="每个账号最多抓取的帖子数量")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=["auto", "browser", "requests"],
        help="抓取模式：auto 优先浏览器会话，browser 强制浏览器，requests 强制请求接口",
    )
    args = parser.parse_args()

    accounts = load_accounts(args.config)
    print(f"[kol_signal] 已加载账号 {len(accounts)} 个")

    posts = []
    warnings: list[str] = []
    for account in accounts:
        fetcher = get_fetcher(account.platform)
        if not fetcher:
            warnings.append(f"{account.display_name}：暂不支持平台 {account.platform}")
            continue
        fetched = fetcher.fetch_recent_posts(account, limit=args.limit, mode=args.mode)
        posts.extend(fetched.posts)
        warnings.extend(fetched.warnings)
        print(
            f"[kol_signal] {account.platform}:{account.display_name} -> "
            f"{len(fetched.posts)} 条，warnings={len(fetched.warnings)}"
        )

    if posts:
        download_post_media(posts)

    report = build_signal_report(accounts, posts)
    report.notes.extend(warnings)
    output_path = write_report(report, Path(args.output))
    print(f"[kol_signal] 报告已写入：{output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
