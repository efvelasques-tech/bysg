from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = ROOT / "backups"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe_reason(reason: str) -> str:
    cleaned = "".join(character if character.isalnum() else "-" for character in reason.lower())
    return "-".join(part for part in cleaned.split("-") if part)[:48] or "manual"


def create_backup(database_path: Path, workbook_path: Path | None = None, reason: str = "manual") -> dict[str, str | int]:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    destination = BACKUP_ROOT / f"{_stamp()}-{_safe_reason(reason)}"
    destination.mkdir(parents=False, exist_ok=False)

    database_copy = destination / "bysg.sqlite3"
    with sqlite3.connect(database_path) as source, sqlite3.connect(database_copy) as target:
        source.backup(target)

    files = [database_copy.name]
    if workbook_path and workbook_path.is_file():
        shutil.copy2(workbook_path, destination / workbook_path.name)
        files.append(workbook_path.name)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reason": reason,
        "files": files,
        "database_bytes": database_copy.stat().st_size,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune()
    return {"name": destination.name, **manifest}


def list_backups(limit: int = 20) -> list[dict[str, str | int]]:
    if not BACKUP_ROOT.exists():
        return []
    result: list[dict[str, str | int]] = []
    for directory in sorted((item for item in BACKUP_ROOT.iterdir() if item.is_dir()), reverse=True):
        manifest_path = directory / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {"created_at": "", "reason": "desconocido", "database_bytes": 0}
        else:
            manifest = {"created_at": "", "reason": "copia previa", "database_bytes": 0}
        result.append({"name": directory.name, **manifest})
        if len(result) >= limit:
            break
    return result


def ensure_daily_backup(database_path: Path, workbook_path: Path | None = None) -> dict[str, str | int] | None:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if any(item.name.startswith(today) for item in BACKUP_ROOT.glob(f"{today}*") if item.is_dir()):
        return None
    return create_backup(database_path, workbook_path, "inicio-diario")


def _prune() -> None:
    keep = max(3, int(os.getenv("BYSG_BACKUP_RETENTION", "30")))
    directories = sorted((item for item in BACKUP_ROOT.iterdir() if item.is_dir()), reverse=True)
    for old in directories[keep:]:
        if ROOT in old.resolve().parents and old.parent.resolve() == BACKUP_ROOT.resolve():
            shutil.rmtree(old)
