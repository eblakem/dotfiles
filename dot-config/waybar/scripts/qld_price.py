#!/usr/bin/env python3
"""Monitor the QLD (QLD1) NEM spot price against a retail plan price cap.

Pulls live 5-minute dispatch price data from the same internal API that
powers AEMO's public NEM data dashboard, then prints a waybar `custom`
module JSON blob: {"text", "tooltip", "class", "percentage"}.

Data source: https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN
(undocumented, but public/unauthenticated - it's what
https://www.aemo.com.au/.../data-dashboard-nem calls client-side).

Price cap context: GloBird Energy's WholeSave plan passes through the raw
NEM wholesale spot price to the customer, with an optional per-kWh cap to
limit exposure to price spikes (see
https://www.globirdenergy.com.au/energy-saver/wholesave/). This script
only tracks the wholesale RRP component in $/kWh against that cap - it
does not attempt to reconstruct the full retail rate (network charges,
losses, GST, etc).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN"
REGION = "QLD1"
REQUEST_TIMEOUT = 10

DEFAULT_CAP = 3.00  # $/kWh
DEFAULT_WARN_PCT = 0.7  # start warning at 70% of the cap

STATE_FILE = os.path.expanduser(
    os.environ.get("QLD_PRICE_STATE_FILE", "~/.cache/qld_price_monitor.json")
)


def fetch_region_rows(region: str) -> list[dict]:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"timeScale": ["30MIN"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        payload = json.load(resp)
    rows = [r for r in payload["5MIN"] if r["REGIONID"] == region]
    rows.sort(key=lambda r: r["SETTLEMENTDATE"])
    return rows


def latest_actual(rows: list[dict]) -> dict | None:
    actual = [r for r in rows if r["PERIODTYPE"] == "ACTUAL"]
    return actual[-1] if actual else None


def today_actual_range(rows: list[dict], now: datetime) -> tuple[float, float] | None:
    today = now.strftime("%Y-%m-%d")
    prices = [
        r["RRP"] / 1000.0
        for r in rows
        if r["PERIODTYPE"] == "ACTUAL" and r["SETTLEMENTDATE"].startswith(today)
    ]
    if not prices:
        return None
    return min(prices), max(prices)


def load_state() -> dict | None:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def classify(price_kwh: float, cap: float, warn_pct: float) -> str:
    if price_kwh >= cap:
        return "critical"
    if price_kwh >= cap * warn_pct:
        return "warning"
    if price_kwh < 0:
        return "negative"
    return "normal"


def format_output(
    price_kwh: float,
    settlement: str,
    cap: float,
    warn_pct: float,
    day_range: tuple[float, float] | None,
    stale: bool,
) -> dict:
    cls = classify(price_kwh, cap, warn_pct)
    if stale:
        cls = "stale"

    text = f"${price_kwh:.3f}/kWh"
    if stale:
        text += " (stale)"

    pct_of_cap = (price_kwh / cap) * 100 if cap else 0

    tooltip_lines = [
        f"QLD1 spot price: ${price_kwh:.4f}/kWh (${price_kwh * 1000:.2f}/MWh)",
        f"Cap: ${cap:.2f}/kWh  ({pct_of_cap:.0f}% of cap)",
        f"Interval: {settlement}",
    ]
    if day_range:
        tooltip_lines.append(f"Today's range: ${day_range[0]:.3f} - ${day_range[1]:.3f}/kWh")
    if stale:
        tooltip_lines.append("Warning: showing last cached value, live fetch failed")

    return {
        "text": text,
        "tooltip": "\n".join(tooltip_lines),
        "class": cls,
        "percentage": max(0, min(100, round(pct_of_cap))),
    }


def run(cap: float, warn_pct: float) -> dict:
    now = datetime.now(timezone.utc).astimezone()
    try:
        rows = fetch_region_rows(REGION)
        latest = latest_actual(rows)
        if latest is None:
            raise ValueError("no ACTUAL rows returned for region")

        price_kwh = latest["RRP"] / 1000.0
        settlement = latest["SETTLEMENTDATE"]
        day_range = today_actual_range(rows, now)

        save_state(
            {
                "price_kwh": price_kwh,
                "settlement": settlement,
                "day_range": day_range,
                "fetched_at": time.time(),
            }
        )
        return format_output(price_kwh, settlement, cap, warn_pct, day_range, stale=False)

    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError) as exc:
        cached = load_state()
        if cached:
            age_min = (time.time() - cached["fetched_at"]) / 60
            out = format_output(
                cached["price_kwh"],
                cached["settlement"],
                cap,
                warn_pct,
                tuple(cached["day_range"]) if cached.get("day_range") else None,
                stale=True,
            )
            out["tooltip"] += f"\nLast good fetch: {age_min:.0f} min ago\nError: {exc}"
            return out
        return {
            "text": "QLD price: error",
            "tooltip": f"Failed to fetch AEMO price data and no cache available.\n{exc}",
            "class": "error",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cap", type=float, default=float(os.environ.get("QLD_PRICE_CAP", DEFAULT_CAP)),
        help="Price cap in $/kWh (default: %(default)s, env QLD_PRICE_CAP)",
    )
    parser.add_argument(
        "--warn-pct", type=float,
        default=float(os.environ.get("QLD_PRICE_WARN_PCT", DEFAULT_WARN_PCT)),
        help="Fraction of cap at which to start warning (default: %(default)s)",
    )
    parser.add_argument(
        "--plain", action="store_true",
        help="Print plain text instead of waybar JSON",
    )
    args = parser.parse_args()

    result = run(args.cap, args.warn_pct)

    if args.plain:
        print(result["text"])
        if "tooltip" in result:
            print(result["tooltip"])
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
