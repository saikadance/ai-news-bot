from __future__ import annotations

import argparse

from browser_session import interactive_login


def main() -> int:
    parser = argparse.ArgumentParser(description="KOL 平台本地登录入口")
    parser.add_argument("--platform", required=True, choices=["weibo", "bilibili"], help="要登录的平台")
    parser.add_argument("--url", default="", help="可选：手动指定登录页或主页地址")
    parser.add_argument("--wait-seconds", type=int, default=180, help="终端不可交互时，自动等待保存会话的秒数")
    args = parser.parse_args()

    result = interactive_login(args.platform, start_url=args.url or None, wait_seconds=args.wait_seconds)
    print("[kol_signal] 登录会话保存结果：")
    for key, value in result.items():
        print(f"  - {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
