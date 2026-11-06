#!/usr/bin/env python3
"""Scrape Miamibloco's Give Miami Day data and write data.js.

Usage:
    python3 scripts/scrape.py
    python3 scripts/scrape.py --dry-run
    python3 scripts/scrape.py --output data.js

The script only uses the Python standard library so it can run in GitHub
Actions (or locally) without installing anything.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

BASE_URL = "https://www.givemiamiday.org"
STORY_URL = (
    f"{BASE_URL}/api/v4/story/Miamibloco.json"
    "?include_all_details=true&requested_type=organization"
)
PAGE_URL = (
    f"{BASE_URL}/api/v4/story/Miamibloco/supporting_fundraisers.json"
    "?page={page}&limit={limit}"
)
SOURCE_URL = f"{BASE_URL}/organization/Miamibloco"

PAGE_SIZE = 6
MAX_PAGES = 200
USER_AGENT = (
    "MiamiblocoDashboard/1.0 "
    "(+https://www.givemiamiday.org/organization/Miamibloco)"
)

INSTRUMENTS = ["Caixa", "Repique", "Surdo", "Tamborim", "Ganzá", "Timbau"]
ROLE_PRIORITY = ["Teaching Artist", "Passista", "Hype Squad"]
HIDDEN_ROLES = {"Student Member", "Hype Squad"}
DEFAULT_ROLE = "Student Member"


def strip_accents(value: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value)
        if unicodedata.category(char) != "Mn"
    )


def fetch_json(url: str, attempts: int = 4) -> dict:
    """Fetch JSON with a few retries and exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as error:
            last_error = error
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def find_instruments(title: str) -> list[str]:
    searchable = strip_accents(title).lower()
    found: list[str] = []

    for instrument in INSTRUMENTS:
        needle = re.escape(strip_accents(instrument).lower())
        if re.search(rf"\b{needle}\b", searchable) and instrument not in found:
            found.append(instrument)

    return found


def find_role(title: str) -> str:
    lower = title.lower()
    for role in ROLE_PRIORITY:
        if role.lower() in lower:
            return role
    return DEFAULT_ROLE


def parse_fundraiser(row: dict) -> dict:
    raw_title = re.sub(r"\s+", " ", row.get("title") or "").strip()
    if not raw_title:
        raise ValueError("fundraiser has no title")

    lower_title = raw_title.lower()
    role = find_role(raw_title)
    instruments = find_instruments(raw_title)
    note = ""

    # A few pages do not follow the usual "Name - Instrument (Role)" shape.
    if lower_title.startswith("geisy"):
        name = "Geisy & JJ"
        note = "mom & son"
    elif lower_title.startswith("michelle-passista"):
        name = "Michelle"
    elif lower_title.startswith("vp keke"):
        name = "VP KeKe"
    else:
        name = re.split(r"\s+-\s+", raw_title, maxsplit=1)[0].strip()

    if not name:
        raise ValueError(f"could not parse a name from {raw_title!r}")

    if not instruments and role not in ("Teaching Artist", "Passista", "Hype Squad"):
        raise ValueError(
            f"could not find a known instrument in {raw_title!r}; "
            f"known instruments: {', '.join(INSTRUMENTS)}"
        )

    urn = row.get("urn")
    if not urn:
        raise ValueError(f"fundraiser {raw_title!r} has no urn")

    record = {
        "name": name,
        "instruments": instruments,
        "role": role,
        "raisedCents": int(row.get("total_amount_raised_in_cents") or 0),
        "goalCents": int(round(float(row.get("goal") or 0) * 100)),
        "urn": urn,
        "photo": row.get("thumbnail") or "",
    }

    if note:
        record["note"] = note

    return record


def fetch_campaign() -> dict:
    story = fetch_json(STORY_URL)

    goal_cents = story.get("goal_amount_in_cents")
    if not goal_cents and story.get("goal"):
        goal_cents = int(round(float(story["goal"]) * 100))

    if not goal_cents:
        raise ValueError("campaign goal is missing from the story response")

    raised_cents = int(story.get("total_amount_raised_in_cents") or 0)

    return {
        "goalCents": int(goal_cents),
        "raisedCents": raised_cents,
        "snapshot": date.today().isoformat(),
        "sourceUrl": SOURCE_URL,
    }


def fetch_fundraisers() -> tuple[list[dict], int | None]:
    fundraisers: dict[str, dict] = {}
    reported_total: int | None = None
    page = 1
    previous_urn_count = 0

    while page <= MAX_PAGES:
        payload = fetch_json(PAGE_URL.format(page=page, limit=PAGE_SIZE))
        rows = payload.get("supporting_fundraisers") or []

        if reported_total is None:
            reported_total = payload.get("total_supporters_count")

        if not rows:
            break

        for row in rows:
            record = parse_fundraiser(row)
            urn = record["urn"]
            if urn in fundraisers:
                raise ValueError(f"duplicate fundraiser urn: {urn}")
            fundraisers[urn] = record

        # The API currently caps at 6 per page. Stop when a short page arrives,
        # or if a page did not add anything (defensive against API changes).
        if len(rows) < PAGE_SIZE or len(fundraisers) == previous_urn_count:
            break

        previous_urn_count = len(fundraisers)
        page += 1
        time.sleep(0.15)

    records = sorted(
        fundraisers.values(),
        key=lambda item: (item["name"].lower(), ",".join(item["instruments"]).lower()),
    )

    if not records:
        raise ValueError("the API returned no fundraisers")

    return records, reported_total


def load_existing(path: Path) -> dict | None:
    """Read a previously generated data.js so unchanged data is not rewritten."""
    try:
        text = path.read_text(encoding="utf-8")
        start = text.index("{")
        end = text.rindex("}")
        return json.loads(text[start : end + 1])
    except (OSError, ValueError):
        return None


def serialise(payload: dict) -> str:
    return "window.DASHBOARD_DATA = " + json.dumps(
        payload, ensure_ascii=False, indent=2
    ) + ";\n"


def section_counts(records: list[dict]) -> Counter:
    counts: Counter = Counter()

    for record in records:
        for instrument in record["instruments"]:
            counts[instrument] += 1
        if record["role"] in ("Teaching Artist", "Passista"):
            counts[record["role"]] += 1

    return counts


def format_money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape Miamibloco's Give Miami Day pages into data.js."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data.js",
        help="output file (default: data.js next to index.html)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print a summary without writing the output file",
    )
    args = parser.parse_args()

    try:
        campaign = fetch_campaign()
        records, reported_total = fetch_fundraisers()
    except Exception as error:  # noqa: BLE001 - surface a clean failure to the runner
        print(f"error: {error}", file=sys.stderr)
        return 1

    payload = {"campaign": campaign, "fundraisers": records}

    # Preserve the previous snapshot date when nothing but the date changed.
    existing = load_existing(args.output)
    if existing:
        old_campaign = dict(existing.get("campaign") or {})
        new_campaign = dict(payload["campaign"])
        old_campaign.pop("snapshot", None)
        new_campaign.pop("snapshot", None)
        if (
            old_campaign == new_campaign
            and existing.get("fundraisers") == payload["fundraisers"]
        ):
            payload["campaign"]["snapshot"] = (
                existing.get("campaign", {}).get("snapshot")
                or payload["campaign"]["snapshot"]
            )

    counts = section_counts(records)
    hidden_roles = Counter(
        record["role"] for record in records if record["role"] in HIDDEN_ROLES
    )

    print(f"Scraped {len(records)} fundraisers")
    if reported_total and reported_total != len(records):
        print(
            f"warning: API reported {reported_total} fundraisers, "
            f"but {len(records)} were returned",
            file=sys.stderr,
        )
    print(
        f"Campaign: {format_money(campaign['raisedCents'])} of "
        f"{format_money(campaign['goalCents'])}"
    )
    print(
        "Sections: "
        + ", ".join(
            f"{name} {counts[name]}"
            for name in INSTRUMENTS + ["Teaching Artist", "Passista"]
        )
    )
    if hidden_roles:
        print(
            "Hidden roles: "
            + ", ".join(
                f"{role} {count}" for role, count in sorted(hidden_roles.items())
            )
        )

    if args.dry_run:
        print("Dry run: no file written")
        return 0

    text = serialise(payload)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
