#!/usr/bin/env python3
"""Send configurable FuelWatch price summaries to a Discord webhook."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


FEED_URL = "https://www.fuelwatch.wa.gov.au/fuelwatch/fuelWatchRSS"
AWST = timezone(timedelta(hours=8), "AWST")
PRODUCTS = {
    1: "Unleaded Petrol",
    2: "Premium Unleaded",
    4: "Diesel",
    5: "LPG",
    6: "98 RON",
    10: "E85",
    11: "Brand diesel",
}
USER_AGENT = "fuelwatch-discord-bot/1.0"
DISCORD_DESCRIPTION_LIMIT = 4096


@dataclass(frozen=True)
class Station:
    price: str
    name: str
    brand: str
    suburb: str
    address: str
    link: str


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    searches = config.get("searches")
    if not isinstance(searches, list) or not searches:
        raise ValueError("config must contain a non-empty 'searches' list")
    if len(searches) > 10:
        raise ValueError("Discord supports at most 10 searches (embeds) per message")
    for index, search in enumerate(searches, 1):
        if not isinstance(search, dict) or not search.get("suburb"):
            raise ValueError(f"search #{index} must have a suburb")
        product = search.get("product")
        if product not in PRODUCTS:
            raise ValueError(f"search #{index} has unsupported product code {product!r}")
    return config


def _text(item: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in item:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in wanted and child.text:
            return child.text.strip()
    return ""


def parse_feed(xml_data: bytes) -> list[Station]:
    root = ET.fromstring(xml_data)
    stations: list[Station] = []
    for item in root.iter("item"):
        title = _text(item, "title")
        title_price, separator, title_name = title.partition(":")
        stations.append(
            Station(
                price=_text(item, "price") or (title_price.strip() if separator else "?"),
                name=_text(item, "trading-name", "tradingname") or title_name.strip() or title,
                brand=_text(item, "brand"),
                suburb=_text(item, "location", "suburb"),
                address=_text(item, "address"),
                link=_text(item, "link"),
            )
        )
    return stations


def fetch(search: dict[str, Any], day: str, timeout: int) -> tuple[list[Station], str]:
    params = {
        "Product": search["product"],
        "Suburb": search["suburb"],
        "Surrounding": "yes" if search.get("surrounding", False) else "no",
        "Day": day,
    }
    url = f"{FEED_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_feed(response.read()), url


def format_description(stations: list[Station], limit: int) -> str:
    if not stations:
        return "No prices were returned for this search."
    lines: list[str] = []
    for number, station in enumerate(stations[:limit], 1):
        place = " — ".join(value for value in (station.name, station.brand) if value)
        location = ", ".join(value for value in (station.address, station.suburb) if value)
        line = f"**{number}. {station.price} c/L** — {place}"
        if location:
            line += f"\n{location}"
        if len("\n\n".join(lines + [line])) > DISCORD_DESCRIPTION_LIMIT:
            break
        lines.append(line)
    return "\n\n".join(lines)


def build_payload(config: dict[str, Any], results: list[tuple[dict[str, Any], list[Station], str]], day: str) -> dict[str, Any]:
    embeds = []
    for search, stations, source_url in results:
        product_name = PRODUCTS[search["product"]]
        embeds.append(
            {
                "title": f"{product_name} — {search['suburb']}",
                "description": format_description(stations, int(search.get("limit", config.get("results_per_search", 5)))),
                "url": source_url,
                "color": 0x2D7D46,
                "footer": {"text": f"FuelWatch WA • {day.title()} prices • Source acknowledged"},
            }
        )
    return {
        "username": config.get("discord_username", "FuelWatch WA"),
        "content": config.get("message", "⛽ FuelWatch price update"),
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


def send_discord(webhook_url: str, payload: dict[str, Any], timeout: int) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status not in (200, 204):
            raise RuntimeError(f"Discord returned HTTP {response.status}")


def run_once(config: dict[str, Any], dry_run: bool = False) -> None:
    day = str(config.get("day", "tomorrow")).lower()
    timeout = int(config.get("request_timeout_seconds", 30))
    results = []
    for search in config["searches"]:
        logging.info("Fetching %s in %s", PRODUCTS[search["product"]], search["suburb"])
        stations, source_url = fetch(search, day, timeout)
        results.append((search, stations, source_url))
    payload = build_payload(config, results, day)
    if dry_run:
        # ASCII escaping keeps previews printable in legacy Windows consoles.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL environment variable is required")
    send_discord(webhook_url, payload, timeout)
    logging.info("Notification sent to Discord")


def next_run(now: datetime, hour: int, minute: int) -> datetime:
    candidate = now.astimezone(AWST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now.astimezone(AWST):
        candidate += timedelta(days=1)
    return candidate


def run_daemon(config: dict[str, Any]) -> None:
    schedule = config.get("schedule", {})
    hour = int(schedule.get("hour", 14))
    minute = int(schedule.get("minute", 40))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule hour/minute is invalid")
    while True:
        now = datetime.now(AWST)
        target = next_run(now, hour, minute)
        wait_seconds = max(1, (target - now).total_seconds())
        logging.info("Next notification: %s (in %.0f seconds)", target.isoformat(), wait_seconds)
        time.sleep(wait_seconds)
        try:
            run_once(config)
        except Exception:
            logging.exception("Scheduled notification failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--once", action="store_true", help="send immediately, then exit")
    parser.add_argument("--dry-run", action="store_true", help="fetch and print the Discord payload without sending")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.once or args.dry_run:
            run_once(config, dry_run=args.dry_run)
        else:
            run_daemon(config)
        return 0
    except (OSError, ValueError, ET.ParseError, urllib.error.URLError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
