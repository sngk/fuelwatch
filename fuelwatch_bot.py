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
DISCORD_EMBEDS_TOTAL_LIMIT = 5500  # Leave headroom below Discord's 6,000-character limit.


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


def _price_number(station: Station) -> float | None:
    try:
        return float(station.price.replace(",", "."))
    except ValueError:
        return None


def _station_key(station: Station) -> tuple[str, str]:
    return (station.name.casefold().strip(), station.address.casefold().strip())


def google_maps_url(station: Station) -> str:
    query = ", ".join(
        value for value in (station.name, station.address, station.suburb, "WA") if value
    )
    return "https://www.google.com/maps/search/?api=1&" + urllib.parse.urlencode({"query": query})


def _price_line(station: Station, number: int, comparison: dict[tuple[str, str], float] | None = None) -> str:
    place = " — ".join(value for value in (station.name, station.brand) if value)
    location = ", ".join(value for value in (station.address, station.suburb) if value)
    line = f"**{number}. {station.price} c/L** — {place}"
    price = _price_number(station)
    previous = comparison.get(_station_key(station)) if comparison and price is not None else None
    if previous is not None and price is not None:
        difference = price - previous
        if abs(difference) < 0.05:
            line += " · **unchanged**"
        elif difference < 0:
            line += f" · **{abs(difference):.1f} c/L cheaper ↓**"
        else:
            line += f" · **{difference:.1f} c/L dearer ↑**"
    if location:
        line += f"\n{location}"
    return line


def format_station_list(stations: list[Station], limit: int, comparison: dict[tuple[str, str], float] | None = None) -> str:
    if not stations:
        return "No prices returned."
    lines: list[str] = []
    for number, station in enumerate(stations[:limit], 1):
        line = _price_line(station, number, comparison)
        if len("\n".join(lines + [line])) > DISCORD_DESCRIPTION_LIMIT:
            break
        lines.append(line)
    return "\n".join(lines)


def format_compact_list(
    stations: list[Station],
    limit: int,
    comparison: dict[tuple[str, str], float] | None = None,
) -> str:
    """Format a short Discord embed column without addresses or repeated labels."""
    if not stations:
        return "_Not available yet_"
    lines: list[str] = []
    for number, station in enumerate(stations[:limit], 1):
        price = _price_number(station)
        change = ""
        previous = comparison.get(_station_key(station)) if comparison and price is not None else None
        if previous is not None and price is not None:
            difference = price - previous
            if abs(difference) < 0.05:
                change = " · ="
            elif difference < 0:
                change = f" · ↓{abs(difference):.1f}"
            else:
                change = f" · ↑{difference:.1f}"
        map_link = google_maps_url(station)
        location = ", ".join(value for value in (station.address, station.suburb.title()) if value)
        lines.append(
            f"`{number}` **{station.price}**{change}\n"
            f"[{station.name}]({map_link})\n"
            f"{location}"
        )
    return "\n\n".join(lines)


def comparison_summary(today: list[Station], tomorrow: list[Station]) -> str:
    today_prices = [(price, station) for station in today if (price := _price_number(station)) is not None]
    tomorrow_prices = [(price, station) for station in tomorrow if (price := _price_number(station)) is not None]
    if not today_prices or not tomorrow_prices:
        return "**Comparison:** unavailable until both days have prices."
    today_price, today_station = min(today_prices, key=lambda value: value[0])
    tomorrow_price, tomorrow_station = min(tomorrow_prices, key=lambda value: value[0])
    difference = tomorrow_price - today_price
    if abs(difference) < 0.05:
        direction = "unchanged"
    elif difference < 0:
        direction = f"**{abs(difference):.1f} c/L cheaper tomorrow ↓**"
    else:
        direction = f"**{difference:.1f} c/L dearer tomorrow ↑**"
    tank_change = abs(difference) * 0.5  # cents/litre × 50 litres, converted to dollars
    detail = ""
    if abs(difference) >= 0.05:
        detail = f" (about **${tank_change:.2f}** on 50 L)"
    return (
        f"**Cheapest comparison:** {direction}{detail}\n"
        f"Today: **{today_price:.1f}** at {today_station.name} · "
        f"Tomorrow: **{tomorrow_price:.1f}** at {tomorrow_station.name}"
    )


def build_payload(
    config: dict[str, Any],
    results: list[tuple[dict[str, Any], list[Station], list[Station], str]],
) -> dict[str, Any]:
    embeds = []
    for search, today, tomorrow, _source_url in results:
        product_name = PRODUCTS[search["product"]]
        limit = int(search.get("limit", config.get("results_per_search", 5)))
        today_by_station = {
            _station_key(station): price
            for station in today
            if (price := _price_number(station)) is not None
        }
        description = (
            f"📍 **{search['suburb'].upper()} + NEARBY**\n"
            f"{comparison_summary(today, tomorrow)}"
        )
        embeds.append(
            {
                "title": f"⛽ {product_name.upper()}",
                "description": description[:DISCORD_DESCRIPTION_LIMIT],
                "color": 0x2D7D46,
                "fields": [
                    {
                        "name": "TODAY",
                        "value": format_compact_list(today, limit),
                        "inline": True,
                    },
                    {
                        "name": "TOMORROW",
                        "value": format_compact_list(tomorrow, limit, today_by_station),
                        "inline": True,
                    },
                ],
                "footer": {"text": "c/L • ↑ dearer • ↓ cheaper • = unchanged • 50 L comparison"},
            }
        )
    message = config.get("message", "⛽ Today's and tomorrow's cheapest fuel prices")
    return {
        "username": config.get("discord_username", "FuelWatch WA"),
        "content": f"{message}\nData: [FuelWatch WA](https://www.fuelwatch.wa.gov.au/)",
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


def _embed_text_length(embed: dict[str, Any]) -> int:
    """Count characters Discord includes in its aggregate embed text limit."""
    total = len(str(embed.get("title", ""))) + len(str(embed.get("description", "")))
    total += len(str(embed.get("footer", {}).get("text", "")))
    total += len(str(embed.get("author", {}).get("name", "")))
    for field in embed.get("fields", []):
        total += len(str(field.get("name", ""))) + len(str(field.get("value", "")))
    return total


def split_discord_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Split embeds into messages that remain safely inside Discord API limits."""
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_length = 0
    for embed in payload.get("embeds", []):
        embed_length = _embed_text_length(embed)
        if current and (current_length + embed_length > DISCORD_EMBEDS_TOTAL_LIMIT or len(current) >= 10):
            groups.append(current)
            current = []
            current_length = 0
        current.append(embed)
        current_length += embed_length
    if current or not groups:
        groups.append(current)

    messages: list[dict[str, Any]] = []
    for index, embeds in enumerate(groups, 1):
        message = {key: value for key, value in payload.items() if key != "embeds"}
        message["embeds"] = embeds
        if index > 1:
            message["content"] = f"⛽ FuelWatch update — continued ({index}/{len(groups)})"
        messages.append(message)
    return messages


def run_once(config: dict[str, Any], dry_run: bool = False) -> None:
    timeout = int(config.get("request_timeout_seconds", 30))
    results = []
    for search in config["searches"]:
        logging.info("Fetching %s in %s", PRODUCTS[search["product"]], search["suburb"])
        today, _ = fetch(search, "today", timeout)
        tomorrow, source_url = fetch(search, "tomorrow", timeout)
        results.append((search, today, tomorrow, source_url))
    payload = build_payload(config, results)
    if dry_run:
        # ASCII escaping keeps previews printable in legacy Windows consoles.
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        raise ValueError("DISCORD_WEBHOOK_URL environment variable is required")
    messages = split_discord_payload(payload)
    for index, message in enumerate(messages, 1):
        send_discord(webhook_url, message, timeout)
        logging.info("Discord message %d/%d sent", index, len(messages))


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
