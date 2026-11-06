#!/usr/bin/env python3
"""Build Puzzle Parlour's date-based challenge catalogs and manifest.

All challenges, including future ones, are published at build time. The Android
app decides which one is unlocked by comparing the *device's local date* to
startsOn <= today < endsBefore; GitHub Actions does not select an active puzzle.
"""

from __future__ import annotations

import calendar
import json
import re
import sys
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHALLENGES = ROOT / "challenges"
SETTINGS_PATH = CHALLENGES / "challenge_settings.json"
MANIFEST_PATH = CHALLENGES / "manifest.json"

KINDS = ("daily", "weekly", "monthly")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# These strings must match the app's PuzzleCutType serialization exactly.
VALID_CUTS = {"classic", "wavy", "u-knob"}

DAILY_RE = re.compile(r"^daily-(\d{4})-(\d{2})-(\d{2})$")
WEEKLY_RE = re.compile(r"^weekly-(\d{4})-[wW](\d{2})$")
MONTHLY_RE = re.compile(r"^monthly-(\d{4})-(\d{2})$")

# Schema 2: 'daily', 'weekly' and 'monthly' in the manifest are ARRAYS, not
# single active objects. Date ranges are LOCAL calendar dates, not instants.
SCHEMA_VERSION = 2
DATE_BASIS = "device-local"


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


def parse_filename(kind: str, image_path: Path) -> dict:
    stem = image_path.stem

    if kind == "daily":
        match = DAILY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected daily-YYYY-MM-DD.<ext>")

        year, month, day = map(int, match.groups())
        start = date(year, month, day)
        end = start + timedelta(days=1)
        title = "Daily"
        period = start.isoformat()

    elif kind == "weekly":
        match = WEEKLY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected weekly-YYYY-wNN.<ext>")

        year, week = map(int, match.groups())
        # ISO week 1 can begin in December of the previous calendar year.
        start = date.fromisocalendar(year, week, 1)
        end = start + timedelta(days=7)
        title = f"Weekly {week}"
        period = f"{year}-W{week:02d}"

    elif kind == "monthly":
        match = MONTHLY_RE.fullmatch(stem)
        if not match:
            raise ValueError(f"{image_path.name}: expected monthly-YYYY-MM.<ext>")

        year, month = map(int, match.groups())
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        title = calendar.month_name[month]
        period = f"{year}-{month:02d}"

    else:
        raise ValueError(f"Unknown challenge kind: {kind}")

    return {
        "id": stem,
        "title": title,
        "period": period,
        # ISO dates, inclusive start and exclusive end. NO UTC offset or zone.
        "startsOn": start.isoformat(),
        "endsBefore": end.isoformat(),
    }


def existing_items(kind: str) -> dict[str, dict]:
    path = CHALLENGES / kind / "catalog.json"
    catalog = load_json(path, {"items": []})
    return {
        item["id"]: item
        for item in catalog.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
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
    cid = item["id"]
    pieces = item.get("pieces")
    if type(pieces) is not int or pieces <= 0:
        raise ValueError(f"{cid}: pieces must be a positive integer")

    seed = item.get("seed")
    if type(seed) is not int or seed <= 0:
        raise ValueError(f"{cid}: seed must be a positive integer")

    cut = item.get("cut")
    if cut not in VALID_CUTS:
        raise ValueError(
            f"{cid}: unsupported cut {cut!r}. Use one of {sorted(VALID_CUTS)} "
            "in challenge_settings.json (defaults or a per-ID override). "
            "Old catalog settings are preserved unless explicitly overridden."
        )

    cut_version = item.get("cutVersion")
    if type(cut_version) is not int or cut_version <= 0:
        raise ValueError(f"{cid}: cutVersion must be a positive integer")

    rotation = item.get("rotationQuarterTurns")
    if type(rotation) is not int or rotation not in (0, 1, 2, 3):
        raise ValueError(f"{cid}: rotationQuarterTurns must be 0, 1, 2 or 3")


def build_catalog(kind: str, settings: dict) -> dict:
    defaults = settings["defaults"][kind]
    overrides = settings.get("overrides", {})
    old_by_id = existing_items(kind)
    generated = []
    seen_periods: dict[str, str] = {}

    for image_path in image_files(kind):
        derived = parse_filename(kind, image_path)
        challenge_id = derived["id"]
        start_date = derived["startsOn"]

        if start_date in seen_periods:
            raise ValueError(
                f"{image_path.name}: duplicate {kind} period with "
                f"{seen_periods[start_date]}; keep only one image per period"
            )
        seen_periods[start_date] = image_path.name

        item = {
            **derived,
            "image": f"images/{image_path.name}",
            "pieces": defaults["pieces"],
            "seed": deterministic_seed(challenge_id),
            "cut": defaults["cut"],
            "cutVersion": defaults.get("cutVersion", 1),
            "rotationQuarterTurns": defaults.get("rotationQuarterTurns", 0),
        }

        # Lock already-published puzzle geometry/settings. Moving to schema 2
        # changes only scheduling fields, not piece layouts or IDs.
        old = old_by_id.get(challenge_id)
        if old:
            for key in (
                "title", "pieces", "seed", "cut", "cutVersion", "rotationQuarterTurns"
            ):
                if key in old:
                    item[key] = old[key]

        # Explicit per-challenge settings win over both defaults and old values.
        override = overrides.get(challenge_id, {})
        for key in (
            "title", "pieces", "seed", "cut", "cutVersion", "rotationQuarterTurns"
        ):
            if key in override:
                item[key] = override[key]

        validate_item(item)
        generated.append(item)

    generated.sort(key=lambda item: (item["startsOn"], item["id"]))
    return {"version": SCHEMA_VERSION, "dateBasis": DATE_BASIS, "items": generated}


def manifest_item(kind: str, item: dict) -> dict:
    result = dict(item)
    # Paths in individual catalogs are relative to challenges/{kind}/.
    # Paths in the root manifest are relative to challenges/.
    result["image"] = f"{kind}/{result['image']}"
    return result


def main() -> int:
    try:
        settings = load_json(SETTINGS_PATH, None)
        if settings is None:
            raise ValueError(f"Missing settings file: {SETTINGS_PATH}")
        if not isinstance(settings, dict):
            raise ValueError("challenge_settings.json must contain a JSON object")

        # Deliberately ignore the old settings['timezone'] (e.g. Europe/Tallinn).
        # An absolute time here would unlock challenges at different local hours.
        catalogs = {kind: build_catalog(kind, settings) for kind in KINDS}
        manifest = {"version": SCHEMA_VERSION, "dateBasis": DATE_BASIS}
        for kind, catalog in catalogs.items():
            manifest[kind] = [manifest_item(kind, item) for item in catalog["items"]]

        # Only write files after ALL kinds have passed validation.
        for kind, catalog in catalogs.items():
            write_json(CHALLENGES / kind / "catalog.json", catalog)
        write_json(MANIFEST_PATH, manifest)

    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Generated version 2 local-date challenge data:")
    for kind in KINDS:
        print(f"  challenges/{kind}/catalog.json ({len(catalogs[kind]['items'])} items)")
    print("  challenges/manifest.json (all published and future items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
