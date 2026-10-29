#!/usr/bin/env python3

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo
import calendar
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
CHALLENGES = ROOT / "challenges"
SETTINGS_PATH = CHALLENGES / "challenge_settings.json"
MANIFEST_PATH = CHALLENGES / "manifest.json"

KINDS = ("daily", "weekly", "monthly")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VALID_CUTS = {"classic", "organic", "angular"}

DAILY_RE = re.compile(r"^daily-(\d{4})-(\d{2})-(\d{2})$")
WEEKLY_RE = re.compile(r"^weekly-(\d{4})-[wW](\d{2})$")
MONTHLY_RE = re.compile(r"^monthly-(\d{4})-(\d{2})$")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")


def deterministic_seed(challenge_id: str) -> int:
    digest = sha256(challenge_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big") % 2_000_000_000
    return max(1, value)


def local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def parse_filename(kind: str, image_path: Path, zone: ZoneInfo) -> dict:
    stem = image_path.stem

    if kind == "daily":
        match = DAILY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected daily-YYYY-MM-DD.<ext>")

        year, month, day = map(int, match.groups())
        start_day = date(year, month, day)
        end_day = start_day + timedelta(days=1)

        return {
            "id": stem,
            "title": "Daily",
            "period": start_day.isoformat(),
            "downloadFrom": local_midnight(start_day, zone).isoformat(),
            "downloadUntil": local_midnight(end_day, zone).isoformat(),
        }

    if kind == "weekly":
        match = WEEKLY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected weekly-YYYY-wNN.<ext>")

        year, week = map(int, match.groups())
        start_day = date.fromisocalendar(year, week, 1)
        end_day = start_day + timedelta(days=7)

        return {
            "id": stem,
            "title": f"Weekly {week}",
            "period": f"{year}-W{week:02d}",
            "downloadFrom": local_midnight(start_day, zone).isoformat(),
            "downloadUntil": local_midnight(end_day, zone).isoformat(),
        }

    if kind == "monthly":
        match = MONTHLY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected monthly-YYYY-MM.<ext>")

        year, month = map(int, match.groups())
        if not 1 <= month <= 12:
            raise ValueError(f"{image_path.name}: invalid month")

        start_day = date(year, month, 1)
        end_day = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        return {
            "id": stem,
            "title": calendar.month_name[month],
            "period": f"{year}-{month:02d}",
            "downloadFrom": local_midnight(start_day, zone).isoformat(),
            "downloadUntil": local_midnight(end_day, zone).isoformat(),
        }

    raise ValueError(f"Unknown challenge kind: {kind}")


def existing_items(kind: str) -> dict[str, dict]:
    catalog_path = CHALLENGES / kind / "catalog.json"
    catalog = load_json(catalog_path, {"version": 1, "items": []})
    return {
        item["id"]: item
        for item in catalog.get("items", [])
        if isinstance(item, dict) and "id" in item
    }


def image_files(kind: str) -> list[Path]:
    folder = CHALLENGES / kind / "images"
    if not folder.exists():
        return []

    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ),
        key=lambda p: p.name.lower(),
    )


def validate_item(item: dict) -> None:
    pieces = item.get("pieces")
    if not isinstance(pieces, int) or pieces <= 0:
        raise ValueError(f"{item.get('id')}: pieces must be a positive integer")

    cut = item.get("cut")
    if cut not in VALID_CUTS:
        raise ValueError(f"{item.get('id')}: cut must be one of {sorted(VALID_CUTS)}")

    rotation = item.get("rotationQuarterTurns")
    if rotation not in (0, 1, 2, 3):
        raise ValueError(
            f"{item.get('id')}: rotationQuarterTurns must be 0, 1, 2 or 3"
        )


def build_catalog(kind: str, settings: dict, zone: ZoneInfo) -> dict:
    defaults = settings["defaults"][kind]
    overrides = settings.get("overrides", {})
    old_by_id = existing_items(kind)
    generated = []

    for image_path in image_files(kind):
        derived = parse_filename(kind, image_path, zone)
        challenge_id = derived["id"]

        item = {
            **derived,
            "image": f"images/{image_path.name}",
            "pieces": defaults["pieces"],
            "seed": deterministic_seed(challenge_id),
            "cut": defaults["cut"],
            "cutVersion": defaults.get("cutVersion", 1),
            "rotationQuarterTurns": defaults.get("rotationQuarterTurns", 0),
        }

        # Existing entries are treated as a lockfile so published puzzle
        # geometry/settings do not silently change when this generator is added.
        old = old_by_id.get(challenge_id)
        if old:
            for key in (
                "title",
                "pieces",
                "seed",
                "cut",
                "cutVersion",
                "rotationQuarterTurns",
            ):
                if key in old:
                    item[key] = old[key]

        # Explicit settings always win.
        override = overrides.get(challenge_id, {})
        for key in (
            "title",
            "pieces",
            "seed",
            "cut",
            "cutVersion",
            "rotationQuarterTurns",
        ):
            if key in override:
                item[key] = override[key]

        validate_item(item)
        generated.append(item)

    generated.sort(key=lambda item: item["downloadFrom"])
    return {"version": 1, "items": generated}


def parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def find_active(catalog: dict, now: datetime) -> dict | None:
    matches = [
        item
        for item in catalog["items"]
        if parse_instant(item["downloadFrom"]) <= now < parse_instant(item["downloadUntil"])
    ]

    if len(matches) > 1:
        ids = ", ".join(item["id"] for item in matches)
        raise RuntimeError(f"Multiple active challenges overlap: {ids}")

    return matches[0] if matches else None


def manifest_item(kind: str, item: dict | None):
    if item is None:
        return None

    result = dict(item)
    result["image"] = f"{kind}/{result['image']}"
    return result


def main() -> int:
    settings = load_json(SETTINGS_PATH, None)
    if settings is None:
        print(f"Missing settings file: {SETTINGS_PATH}", file=sys.stderr)
        return 1

    try:
        zone = ZoneInfo(settings.get("timezone", "Europe/Tallinn"))
    except Exception as exc:
        print(f"Invalid timezone: {exc}", file=sys.stderr)
        return 1

    catalogs = {}

    try:
        for kind in KINDS:
            catalog = build_catalog(kind, settings, zone)
            catalogs[kind] = catalog
            write_json(CHALLENGES / kind / "catalog.json", catalog)
    except (ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    manifest = {"version": 1}

    for kind in KINDS:
        active = find_active(catalogs[kind], now)
        if active is None:
            print(f"WARNING: no active {kind} challenge at {now.isoformat()}")
            manifest[kind] = None
        else:
            manifest[kind] = manifest_item(kind, active)

    write_json(MANIFEST_PATH, manifest)

    print("Generated:")
    for kind in KINDS:
        print(f"  challenges/{kind}/catalog.json")
    print("  challenges/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
