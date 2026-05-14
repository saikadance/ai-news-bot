from __future__ import annotations

import json
from pathlib import Path

from models import KOLAccount, SignalReport


MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_DIR / "output"
MEDIA_DIR = OUTPUT_DIR / "media"
DEFAULT_CONFIG = MODULE_DIR / "accounts.json"
DEFAULT_REPORT = MODULE_DIR / "latest_report.json"


def load_accounts(config_path: str | Path) -> list[KOLAccount]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"账号配置不存在：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("账号配置文件必须是数组")
    accounts: list[KOLAccount] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        accounts.append(
            KOLAccount(
                platform=str(item.get("platform", "")).strip(),
                account_id=str(item.get("account_id", "")).strip(),
                display_name=str(item.get("display_name", "")).strip(),
                homepage=str(item.get("homepage", "")).strip(),
                priority=int(item.get("priority", 1) or 1),
                tags=[str(x).strip() for x in item.get("tags", []) if str(x).strip()],
                focus_keywords=[
                    str(x).strip() for x in item.get("focus_keywords", []) if str(x).strip()
                ],
                require_image_review=bool(item.get("require_image_review", False)),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return accounts


def write_report(report: SignalReport, output_path: str | Path = DEFAULT_REPORT) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path
