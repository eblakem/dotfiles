#!/usr/bin/env python3
"""Monitor the QLD (QLD1) NEM spot price against a retail plan price cap.

Pulls live 5-minute dispatch price data from the same internal API that
powers AEMO's public NEM data dashboard, then prints a waybar `custom`
module JSON blob: {"text", "tooltip", "class", "percentage"}.

Data source: https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN
(undocumented, but public/unauthenticated - it's what
https://www.aemo.com.au/.../data-dashboard-nem calls client-side).

Price cap context: GloBird Energy's WholeSave plan passes through the raw
NEM wholesale spot price to the customer, with a purchased per-kWh cap to
limit exposure to price spikes (see
https://www.globirdenergy.com.au/energy-saver/wholesave/). Defaults below
are taken verbatim from the account's WholeSave Terms & Conditions, Energy
Plan and Cap Confirmation documents: $1.00/kWh wholesale price cap (excl.
GST) on a 3.00kW cap demand, daily charge, membership fee and cap fee
(GST-inclusive per the price table), plus the account's Distribution/
Marginal Loss Factors. Per T&Cs paragraph 8, GST is only added to the
Wholesale Usage Charge (the spot-price energy component) - the daily/
membership/cap charges are already GST-inclusive and are not taxed again.
Also includes a Peak/Off-peak network/distribution charge, billed on top of
the Wholesale Usage Charge - confirmed empirically against real bills, not
from an explicit T&Cs clause; see
~/braindump/personal/wholesave-globird/README.md for the full derivation.
Controlled load is NOT modelled as a separate charge (T&Cs paragraph 5
bills it as part of general usage under WholeSave). The estimated hourly
cost is still a rough approximation: it assumes a constant load rather than
actual metered per-interval usage - for that, use qld_price_history.py or
qld_price_tui.py, which can import real per-interval data.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

API_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/5MIN"
REGION = "QLD1"
REQUEST_TIMEOUT = 10

DEFAULT_CAP = 1.00  # Wholesale Price Cap ($/kWh, excl. GST)
DEFAULT_WARN_PCT = 0.7  # start warning at 70% of the cap

DEFAULT_CAP_DEMAND_KW = 3.00  # Wholesale Cap Demand (kW)
DEFAULT_CAP_FEE_PER_KW_DAY = 0.209  # Wholesale Cap Fee ($/kW/day, incl. GST)
DEFAULT_DAILY_CHARGE = 1.254  # Daily Charge ($/day, incl. GST)
DEFAULT_MEMBERSHIP_FEE = 0.72127  # Membership Fee ($/day, incl. GST)

# Distribution/Marginal Loss Factors applied to the Wholesale Usage Charge
# (T&Cs paragraph 5's WUC formula multiplies the weighted-average-price x
# usage figure by DLF x MLF) - from the Welcome Pack's account-specific
# loss factor table.
DEFAULT_DLF = 1.05034  # Distribution Loss Factor
DEFAULT_MLF = 1.013  # Marginal Loss Factor

# General (spot-exposed) usage from the most expensive bill on record - GloBird
# invoice 10677636, 05-May-2026 to 01-Jun-2026 (28 days), pre-WholeSave BOOST
# plan: peak + offpeak + shoulder = 557.91 kWh -> used as a worst-case load.
DEFAULT_LOAD_KWH_PER_DAY = 557.91 / 28

# Controlled load (e.g. hot water): on the pre-WholeSave BOOST plan (same
# bill as above: 204.51 kWh / 28 days) it was billed flat, separately from
# general usage. Under WholeSave it is NOT separate - T&Cs paragraph 5
# defines the wholesale-billed usage (Un) as including "any controlled
# load, if applicable", and this is confirmed empirically: adding a flat
# controlled-load charge on top of real per-interval WholeSave billing data
# overshot two actual bills by ~$0.52/day; without it they're within
# ~1.5%. So the default here is 0 kWh/day for this plan - the fields still
# exist (and DEFAULT_CONTROLLED_LOAD_RATE is kept) in case a different
# account/plan does bill it separately.
DEFAULT_CONTROLLED_LOAD_KWH_PER_DAY = 0.0
DEFAULT_CONTROLLED_LOAD_RATE = 0.03740  # Controlled Load ($/kWh, incl. GST)

# Network/distribution charge from the account's WholeSave price table -
# billed *in addition to* the Wholesale Usage Charge, not instead of it (the
# price table lists it alongside Daily Charge/Membership Fee with no formula
# reference, unlike Wholesale Usage Charge which points to T&Cs paragraphs
# 5-8). Confirmed empirically: Peak kWh x this rate + Off-peak kWh x this
# rate accounts for the gap between modelled and actual bills to within ~1
# cent across two independent days (05-06 Aug 2026) - see
# ~/braindump/personal/wholesave-globird/README.md. No published Shoulder
# rate, so it defaults to 0.
DEFAULT_PEAK_NETWORK_RATE = 0.18920  # Peak Usage ($/kWh, incl. GST)
DEFAULT_OFFPEAK_NETWORK_RATE = 0.05170  # Off-peak Usage ($/kWh, incl. GST)
DEFAULT_SHOULDER_NETWORK_RATE = 0.0  # no published Shoulder rate

DEFAULT_GST_RATE = 0.10


@dataclass
class Plan:
    cap_demand_kw: float
    cap_fee_per_kw_day: float
    daily_charge: float
    membership_fee: float
    load_kw: float
    controlled_load_kwh_per_day: float
    controlled_load_rate: float
    gst_rate: float
    dlf: float = DEFAULT_DLF
    mlf: float = DEFAULT_MLF
    peak_network_rate: float = DEFAULT_PEAK_NETWORK_RATE
    offpeak_network_rate: float = DEFAULT_OFFPEAK_NETWORK_RATE
    shoulder_network_rate: float = DEFAULT_SHOULDER_NETWORK_RATE

    @property
    def fixed_cost_per_hour(self) -> float:
        # Daily charge, membership fee, cap fee and controlled load rate are
        # all GST-inclusive already (per the WholeSave price table), so this
        # figure needs no further GST applied.
        daily_fixed = (
            self.daily_charge
            + self.membership_fee
            + (self.cap_demand_kw * self.cap_fee_per_kw_day)
            + (self.controlled_load_kwh_per_day * self.controlled_load_rate)
        )
        return daily_fixed / 24

    def estimate_hourly_cost(self, price_kwh: float, cap: float, hour: int) -> tuple[float, float]:
        # The cap only protects usage while demand stays within cap_demand_kw
        # (e.g. a 3kW cap means under 0.25kWh per 5-min interval). Exceed that
        # and the interval is billed at the full uncapped spot price.
        within_cap_demand = self.load_kw <= self.cap_demand_kw
        effective_price = (
            min(price_kwh, cap) if price_kwh > 0 and within_cap_demand else price_kwh
        )
        # Wholesale Usage Charge = effective price x usage x DLF x MLF (T&Cs
        # paragraph 5), and only this energy component is GST-exclusive
        # (paragraph 8) - the fixed charges above are already GST-inclusive.
        wholesale_usage_charge = effective_price * self.load_kw * self.dlf * self.mlf
        net_charge = self.load_kw * network_rate_for_period(tou_period_for_hour(hour), self)
        total = self.fixed_cost_per_hour + wholesale_usage_charge * (1 + self.gst_rate) + net_charge
        return total, effective_price


# Account-specific Time of Use windows (from the account's GloBird app/bill,
# QLD - confirmed directly; the T&Cs only point to an external, ambiguous
# page for these). Controlled load has no TOU window of its own - see
# DEFAULT_CONTROLLED_LOAD_KWH_PER_DAY.
TOU_PEAK_HOURS = range(16, 21)  # 16:00-21:00
TOU_OFFPEAK_HOURS = range(9, 16)  # 09:00-16:00
TOU_SHOULDER_HOURS = list(range(21, 24)) + list(range(0, 9))  # 21:00-09:00


def tou_period_for_hour(hour: int) -> str:
    """Which of "peak"/"offpeak"/"shoulder" a clock hour (0-23) falls into,
    per this account's confirmed TOU windows above."""
    if hour in TOU_PEAK_HOURS:
        return "peak"
    if hour in TOU_OFFPEAK_HOURS:
        return "offpeak"
    return "shoulder"


def network_rate_for_period(period: str, plan: Plan) -> float:
    return {
        "peak": plan.peak_network_rate,
        "offpeak": plan.offpeak_network_rate,
        "shoulder": plan.shoulder_network_rate,
    }[period]


def network_charge(peak_kwh: float, offpeak_kwh: float, shoulder_kwh: float, plan: Plan) -> float:
    """Network/distribution charge for a TOU-split amount of usage -
    GST-inclusive already (see DEFAULT_PEAK_NETWORK_RATE etc above), so
    unlike the Wholesale Usage Charge no further GST is added here."""
    return (
        peak_kwh * plan.peak_network_rate
        + offpeak_kwh * plan.offpeak_network_rate
        + shoulder_kwh * plan.shoulder_network_rate
    )

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


def current_hour_average(rows: list[dict], now: datetime) -> float | None:
    hour_prefix = now.strftime("%Y-%m-%dT%H")
    prices = [
        r["RRP"] / 1000.0
        for r in rows
        if r["PERIODTYPE"] == "ACTUAL" and r["SETTLEMENTDATE"].startswith(hour_prefix)
    ]
    if not prices:
        return None
    return sum(prices) / len(prices)


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
    hour_avg: float | None,
    plan: Plan,
    stale: bool,
) -> dict:
    cls = classify(price_kwh, cap, warn_pct)
    if stale:
        cls = "stale"

    hour = int(settlement[11:13])
    period = tou_period_for_hour(hour)
    est_hourly_cost, effective_price = plan.estimate_hourly_cost(price_kwh, cap, hour)

    text = f"${price_kwh:.3f}/kWh · ~${est_hourly_cost:.2f}/h"
    if stale:
        text += " (stale)"

    pct_of_cap = (price_kwh / cap) * 100 if cap else 0

    tooltip_lines = [
        f"QLD1 spot price: ${price_kwh:.4f}/kWh (${price_kwh * 1000:.2f}/MWh)",
        f"Cap: ${cap:.2f}/kWh  ({pct_of_cap:.0f}% of cap)",
        f"Interval: {settlement}  ({period})",
    ]
    if hour_avg is not None:
        tooltip_lines.append(f"Current hour average: ${hour_avg:.4f}/kWh")
    if day_range:
        tooltip_lines.append(f"Today's range: ${day_range[0]:.3f} - ${day_range[1]:.3f}/kWh")
    fixed_line = (
        f"Fixed charges: ${plan.daily_charge:.2f} daily + ${plan.membership_fee:.2f} membership "
        f"+ ${plan.cap_demand_kw:.1f}kW × ${plan.cap_fee_per_kw_day:.3f} cap fee"
    )
    if plan.controlled_load_kwh_per_day:
        fixed_line += (
            f" + {plan.controlled_load_kwh_per_day:.2f}kWh × ${plan.controlled_load_rate:.4f} controlled load"
        )
    fixed_line += f" = ${plan.fixed_cost_per_hour * 24:.2f}/day (${plan.fixed_cost_per_hour:.3f}/h)"
    tooltip_lines.append(fixed_line)
    tooltip_lines.append(
        f"Network charge ({period}): ${network_rate_for_period(period, plan):.4f}/kWh"
    )
    if effective_price < price_kwh:
        tooltip_lines.append(f"Capped wholesale rate applied: ${effective_price:.4f}/kWh")
    elif plan.load_kw > plan.cap_demand_kw:
        tooltip_lines.append(
            f"Warning: assumed load {plan.load_kw:.2f}kW exceeds the {plan.cap_demand_kw:.1f}kW "
            "cap demand threshold - cap does not apply at this load"
        )
    tooltip_lines.append(
        f"Est. cost @ {plan.load_kw:.2f}kW avg load, incl. {plan.gst_rate * 100:.0f}% GST: "
        f"${est_hourly_cost:.2f}/h"
    )
    if stale:
        tooltip_lines.append("Warning: showing last cached value, live fetch failed")

    return {
        "text": text,
        "tooltip": "\n".join(tooltip_lines),
        "class": cls,
        "percentage": max(0, min(100, round(pct_of_cap))),
    }


def run(cap: float, warn_pct: float, plan: Plan) -> dict:
    now = datetime.now(timezone.utc).astimezone()
    try:
        rows = fetch_region_rows(REGION)
        latest = latest_actual(rows)
        if latest is None:
            raise ValueError("no ACTUAL rows returned for region")

        price_kwh = latest["RRP"] / 1000.0
        settlement = latest["SETTLEMENTDATE"]
        day_range = today_actual_range(rows, now)
        hour_avg = current_hour_average(rows, now)

        save_state(
            {
                "price_kwh": price_kwh,
                "settlement": settlement,
                "day_range": day_range,
                "hour_avg": hour_avg,
                "fetched_at": time.time(),
            }
        )
        return format_output(
            price_kwh, settlement, cap, warn_pct, day_range, hour_avg, plan, stale=False
        )

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
                cached.get("hour_avg"),
                plan,
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
        "--cap-demand-kw", type=float,
        default=float(os.environ.get("QLD_CAP_DEMAND_KW", DEFAULT_CAP_DEMAND_KW)),
        help="Purchased wholesale cap demand in kW (default: %(default)s, env QLD_CAP_DEMAND_KW)",
    )
    parser.add_argument(
        "--cap-fee-per-kw-day", type=float,
        default=float(os.environ.get("QLD_CAP_FEE_PER_KW_DAY", DEFAULT_CAP_FEE_PER_KW_DAY)),
        help="Wholesale cap fee in $/kW/day (default: %(default)s)",
    )
    parser.add_argument(
        "--daily-charge", type=float,
        default=float(os.environ.get("QLD_DAILY_CHARGE", DEFAULT_DAILY_CHARGE)),
        help="Daily charge in $/day (default: %(default)s)",
    )
    parser.add_argument(
        "--membership-fee", type=float,
        default=float(os.environ.get("QLD_MEMBERSHIP_FEE", DEFAULT_MEMBERSHIP_FEE)),
        help="Membership fee in $/day (default: %(default)s)",
    )
    parser.add_argument(
        "--load-kwh-per-day", type=float,
        default=float(os.environ.get("QLD_LOAD_KWH_PER_DAY", DEFAULT_LOAD_KWH_PER_DAY)),
        help="Assumed spot-exposed general usage in kWh/day, used as a constant kW "
             "load for the hourly cost estimate (default: %(default).2f, from most "
             "expensive bill on record, env QLD_LOAD_KWH_PER_DAY)",
    )
    parser.add_argument(
        "--controlled-load-kwh-per-day", type=float,
        default=float(
            os.environ.get(
                "QLD_CONTROLLED_LOAD_KWH_PER_DAY", DEFAULT_CONTROLLED_LOAD_KWH_PER_DAY
            )
        ),
        help="Controlled load usage in kWh/day, billed flat (not spot-exposed) "
             "(default: %(default).2f, env QLD_CONTROLLED_LOAD_KWH_PER_DAY)",
    )
    parser.add_argument(
        "--controlled-load-rate", type=float,
        default=float(
            os.environ.get("QLD_CONTROLLED_LOAD_RATE", DEFAULT_CONTROLLED_LOAD_RATE)
        ),
        help="Controlled load rate in $/kWh (default: %(default)s, "
             "env QLD_CONTROLLED_LOAD_RATE)",
    )
    parser.add_argument(
        "--gst-rate", type=float,
        default=float(os.environ.get("QLD_GST_RATE", DEFAULT_GST_RATE)),
        help="GST rate applied to the wholesale usage (energy) charge only (default: %(default)s)",
    )
    parser.add_argument(
        "--dlf", type=float, default=float(os.environ.get("QLD_DLF", DEFAULT_DLF)),
        help="Distribution Loss Factor applied to the wholesale usage charge (default: %(default)s, env QLD_DLF)",
    )
    parser.add_argument(
        "--mlf", type=float, default=float(os.environ.get("QLD_MLF", DEFAULT_MLF)),
        help="Marginal Loss Factor applied to the wholesale usage charge (default: %(default)s, env QLD_MLF)",
    )
    parser.add_argument(
        "--peak-network-rate", type=float,
        default=float(os.environ.get("QLD_PEAK_NETWORK_RATE", DEFAULT_PEAK_NETWORK_RATE)),
        help="Network/distribution charge for Peak usage, $/kWh, on top of the wholesale charge "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--offpeak-network-rate", type=float,
        default=float(os.environ.get("QLD_OFFPEAK_NETWORK_RATE", DEFAULT_OFFPEAK_NETWORK_RATE)),
        help="Network/distribution charge for Off-peak usage, $/kWh (default: %(default)s)",
    )
    parser.add_argument(
        "--shoulder-network-rate", type=float,
        default=float(os.environ.get("QLD_SHOULDER_NETWORK_RATE", DEFAULT_SHOULDER_NETWORK_RATE)),
        help="Network/distribution charge for Shoulder usage, $/kWh (default: %(default)s)",
    )
    parser.add_argument(
        "--plain", action="store_true",
        help="Print plain text instead of waybar JSON",
    )
    args = parser.parse_args()

    plan = Plan(
        cap_demand_kw=args.cap_demand_kw,
        cap_fee_per_kw_day=args.cap_fee_per_kw_day,
        daily_charge=args.daily_charge,
        membership_fee=args.membership_fee,
        load_kw=args.load_kwh_per_day / 24,
        controlled_load_kwh_per_day=args.controlled_load_kwh_per_day,
        controlled_load_rate=args.controlled_load_rate,
        gst_rate=args.gst_rate,
        dlf=args.dlf,
        mlf=args.mlf,
        peak_network_rate=args.peak_network_rate,
        offpeak_network_rate=args.offpeak_network_rate,
        shoulder_network_rate=args.shoulder_network_rate,
    )
    result = run(args.cap, args.warn_pct, plan)

    if args.plain:
        print(result["text"])
        if "tooltip" in result:
            print(result["tooltip"])
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
