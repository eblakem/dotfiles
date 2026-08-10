#!/usr/bin/env python3
"""Interactive terminal UI for fact-checking a retailer's electricity bill
against AEMO's published wholesale spot price.

A curses front-end over qld_price_history.py (reuses its data-fetching and
cost-estimate logic, and qld_price.py's Plan model for fixed charges/GST/
price cap - see those two scripts' docstrings for the underlying data
source and billing model). Move through days and hours with the arrow
keys, watch the day's price chart, and type in usage/cost as you read
them off a bill - the estimate and the actual-vs-estimate comparison
update live. Entries you save (`s`) are kept in a small local JSON log
(see HISTORY_FILE) so you can flip back through days you've already
checked with `[` / `]`.

If your retailer's portal can export actual per-interval usage (a GloBird
"Wholesale Data Export" CSV: Date, Time, Usage (kWh), Wholesale Price,
DLF, MLF, Wholesale Usage Charge), drop it in ACTUAL_DATA_DIR and it's
picked up automatically (and on `r`): whichever day/hour you're viewing
gets an "Actual (imported)" line using the exact real usage, per-interval
Wholesale Usage Charge, and Peak/Off-peak network charge for that window -
no modelling, no flat-load assumption, no manual entry needed. The Usage
[u] field is pre-filled from it too, for the fallback flat estimate on
days without an import.

Press `m` for a month summary page: a table of every saved day's whole-day
usage/cost, that day's average AEMO wholesale spot price, and (where actual
import data covers the day) its average 5-minute-interval usage, for the
currently viewed month (←/→ to change month), with `e` to export it as a
Markdown table (see summary_export_path()).

Run:
  qld_price_tui.py                 # opens on yesterday, QLD1
  qld_price_tui.py 06-08-2026      # opens on a specific day
  qld_price_tui.py --region NSW1
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import textwrap
import urllib.error
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import qld_globird_fetch as qgf
import qld_price as qp
import qld_price_history as qph

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]

HISTORY_FILE = os.path.expanduser(
    os.environ.get(
        "QLD_HISTORY_FILE", "~/braindump/personal/wholesave-globird/qld_price_history_log.json"
    )
)

SPARK = " ▁▂▃▄▅▆▇█"

# curses color-pair numbers for the peak/off-peak/shoulder TOU meters (see
# qp.tou_period_for_hour()) - initialized in App.run(), used by both
# draw_hour_table() and draw_usage_cost() so a given meter reads as the
# same color everywhere.
TOU_COLOR_PAIR = {"peak": 5, "offpeak": 6, "shoulder": 7}

PLAN = qp.Plan(
    cap_demand_kw=qp.DEFAULT_CAP_DEMAND_KW,
    cap_fee_per_kw_day=qp.DEFAULT_CAP_FEE_PER_KW_DAY,
    daily_charge=qp.DEFAULT_DAILY_CHARGE,
    membership_fee=qp.DEFAULT_MEMBERSHIP_FEE,
    load_kw=0.0,
    controlled_load_kwh_per_day=qp.DEFAULT_CONTROLLED_LOAD_KWH_PER_DAY,
    controlled_load_rate=qp.DEFAULT_CONTROLLED_LOAD_RATE,
    gst_rate=qp.DEFAULT_GST_RATE,
    dlf=qp.DEFAULT_DLF,
    mlf=qp.DEFAULT_MLF,
    peak_network_rate=qp.DEFAULT_PEAK_NETWORK_RATE,
    offpeak_network_rate=qp.DEFAULT_OFFPEAK_NETWORK_RATE,
    shoulder_network_rate=qp.DEFAULT_SHOULDER_NETWORK_RATE,
)
CAP = qp.DEFAULT_CAP

# WholeSave's Billing Period is a fixed 28 days (Welcome Pack para 5), not a
# calendar month, and doesn't reset to a fixed day-of-month - it rolls
# forward in exact 28-day blocks from when the plan took effect. Anchored at
# the WholeSave plan's start date (Welcome Pack: "should take effect from 05
# Aug 2026"); the account's first cycle (05 Aug - 01 Sep 2026) confirms it.
BILLING_CYCLE_ANCHOR = date(2026, 8, 5)
BILLING_CYCLE_DAYS = 28


# ---- pure helpers (no curses) -----------------------------------------


def parse_float(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fmt_opt(v: float | None) -> str:
    return "" if v is None else f"{v:g}"


def month_days(year: int, month: int) -> list[date]:
    first = date(year, month, 1)
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days, d = [], first
    while d < next_first:
        days.append(d)
        d += timedelta(days=1)
    return days


def interval_prices_from_rows(rows: list[tuple[str, float]]) -> dict[str, float]:
    """Reduces AEMO (SETTLEMENTDATE, RRP $/MWh) rows (as returned by
    qld_price_history.fetch_month()) to {SETTLEMENTDATE: price $/kWh}, in
    the same raw, GST-exclusive convention as qld_price.DEFAULT_CAP - so it
    can be compared directly against CAP interval-by-interval."""
    return {ts: rrp / 1000.0 for ts, rrp in rows}


def _interval_lost_cap(
    usage: float, ts: str, cap_kwh_per_interval: float, interval_prices: dict[str, float] | None
) -> bool:
    """Whether a 5-min interval actually lost the wholesale price cap's
    protection - i.e. cost something - not just whether demand exceeded
    PLAN.cap_demand_kw. Per qld_price.Plan.estimate_hourly_cost():
    effective_price = min(price_kwh, cap) when within the demand cap, else
    price_kwh outright. If price_kwh was already <= cap, that's the same
    number either way - exceeding the *demand* cap only actually costs
    something when the AEMO spot price for that same interval was itself
    spiking above CAP. `interval_prices` missing this timestamp (fetch
    failed, or a very recent interval not yet in AEMO's published archive)
    defaults to "no", per the same reasoning: don't claim a spike without
    the data to back it."""
    if usage <= cap_kwh_per_interval:
        return False
    if interval_prices is None:
        return True
    price = interval_prices.get(ts)
    return price is not None and price > CAP


def day_interval_stats(
    actual_data: dict[str, tuple[float, float]],
    year: int,
    month: int,
    interval_prices: dict[str, float] | None = None,
) -> dict[date, dict]:
    """Per-calendar-day average 5-minute-interval usage (kWh) in
    `year`/`month`, computed directly from the real imported readings in
    `actual_data` (see qld_price_history.sync_actual_data()) - not day-total
    usage divided by an assumed interval count, so it stays exact even for a
    day with only partial import coverage. Each entry also counts how many
    of that day's intervals actually lost PLAN's demand-cap protection (see
    _interval_lost_cap() - demand over cap_demand_kw *and* the AEMO spot
    price for that interval itself over CAP, from `interval_prices` if
    given; otherwise demand alone, same as before)."""
    buckets: dict[date, list[tuple[str, float]]] = {}
    for ts, (usage, _charge) in actual_data.items():
        d = datetime.strptime(ts[:10], "%Y/%m/%d").date()
        if d.year == year and d.month == month:
            buckets.setdefault(d, []).append((ts, usage))
    cap_kwh_per_interval = PLAN.cap_demand_kw / 12
    stats = {}
    for d, items in buckets.items():
        usages = [u for _ts, u in items]
        stats[d] = {
            "avg": sum(usages) / len(usages),
            "over_cap": sum(1 for ts, u in items if _interval_lost_cap(u, ts, cap_kwh_per_interval, interval_prices)),
        }
    return stats


def month_over_cap_ranges(
    actual_data: dict[str, tuple[float, float]],
    year: int,
    month: int,
    interval_prices: dict[str, float] | None = None,
) -> list[tuple[str, str, int, int]]:
    """Which clock-time windows commonly cost something by pushing usage
    over PLAN's demand cap while the AEMO spot price was also spiking (see
    _interval_lost_cap()) this month: for every 5-min interval time-of-day
    (e.g. "17:35") that lost cap protection on at least one day, how many
    distinct days it happened on and how many times total - then merges
    consecutive 5-min slots that share the same day-count into a single
    (start, end, day_count, total_count) range, so a sustained recurring
    block (e.g. "same ~90min every evening") reads as one row instead of a
    dozen near-identical ones. Sorted by day-count then start time
    descending/ascending respectively, so the most persistent, widest-
    reaching windows surface first - that kind of pattern points at a
    specific appliance/habit, unlike a single day's one-off spike
    (day_count 1, which still appears, just ranked last)."""
    cap_kwh_per_interval = PLAN.cap_demand_kw / 12
    days_by_time: dict[str, set[date]] = {}
    counts_by_time: dict[str, int] = {}
    for ts, (usage, _charge) in actual_data.items():
        dt = datetime.strptime(ts, "%Y/%m/%d %H:%M:%S")
        if dt.year != year or dt.month != month:
            continue
        if not _interval_lost_cap(usage, ts, cap_kwh_per_interval, interval_prices):
            continue
        clock = dt.strftime("%H:%M")
        days_by_time.setdefault(clock, set()).add(dt.date())
        counts_by_time[clock] = counts_by_time.get(clock, 0) + 1

    def to_minutes(clock: str) -> int:
        h, m = clock.split(":")
        return int(h) * 60 + int(m)

    ordered = sorted(counts_by_time, key=to_minutes)
    ranges: list[dict] = []
    for clock in ordered:
        day_count = len(days_by_time[clock])
        mins = to_minutes(clock)
        cur = ranges[-1] if ranges else None
        if cur and cur["day_count"] == day_count and mins - to_minutes(cur["end"]) == 5:
            cur["end"] = clock
            cur["total"] += counts_by_time[clock]
        else:
            ranges.append({"start": clock, "end": clock, "day_count": day_count, "total": counts_by_time[clock]})

    result = [(r["start"], r["end"], r["day_count"], r["total"]) for r in ranges]
    result.sort(key=lambda r: (-r[2], r[0]))
    return result


def month_summary_rows(
    history: dict,
    region: str,
    year: int,
    month: int,
    interval_stats: dict[date, dict] | None = None,
) -> list[dict]:
    """Whole-day usage/cost for each saved day in `year`/`month`, pulled from
    the "day" bucket (see set_entry()/App.save() - populated either by
    saving in all-day view, or via the U/C whole-day fields from an hour
    view). Days with neither saved are skipped. `interval_stats` (from
    day_interval_stats()) fills in each row's average 5-minute-interval
    usage and demand-cap-exceedance count, when actual import data covers
    that day."""
    region_entry = history.get(region, {})
    interval_stats = interval_stats or {}
    rows = []
    for d in month_days(year, month):
        day_bucket = region_entry.get(d.isoformat(), {}).get("day", {})
        usage, cost = day_bucket.get("usage"), day_bucket.get("cost")
        if usage is None and cost is None:
            continue
        stats = interval_stats.get(d, {})
        rows.append(
            {
                "date": d,
                "usage": usage,
                "cost": cost,
                "interval_avg": stats.get("avg"),
                "interval_over_cap": stats.get("over_cap"),
            }
        )
    return rows


def billing_cycle_containing(d: date) -> tuple[date, date]:
    """The (start, end) dates (inclusive) of the BILLING_CYCLE_DAYS-day
    billing cycle that contains `d`, per BILLING_CYCLE_ANCHOR - rolls
    forward in fixed-length blocks with no calendar alignment, so it drifts
    through the month over successive cycles (unlike a calendar month)."""
    cycle_index = (d - BILLING_CYCLE_ANCHOR).days // BILLING_CYCLE_DAYS
    start = BILLING_CYCLE_ANCHOR + timedelta(days=cycle_index * BILLING_CYCLE_DAYS)
    end = start + timedelta(days=BILLING_CYCLE_DAYS - 1)
    return start, end


def predicted_cycle_cost(
    history: dict, region: str
) -> tuple[float, int, int, date, date] | None:
    """Run-rate projection for the *current* billing cycle (see
    billing_cycle_containing) - not the calendar month, since WholeSave's
    28-day Billing Period doesn't align to one and can span two calendar
    months. Averages billed cost across whichever days in the current cycle
    already have a saved cost, extrapolated across the full cycle length.
    Returns (predicted_total, days_with_cost, cycle_days, cycle_start,
    cycle_end), or None if no day in the current cycle has a saved cost
    yet."""
    start, end = billing_cycle_containing(date.today())
    region_entry = history.get(region, {})
    costs = []
    d = start
    while d <= end:
        cost = region_entry.get(d.isoformat(), {}).get("day", {}).get("cost")
        if cost is not None:
            costs.append(cost)
        d += timedelta(days=1)
    if not costs:
        return None
    cycle_days = (end - start).days + 1
    predicted_total = (sum(costs) / len(costs)) * cycle_days
    return predicted_total, len(costs), cycle_days, start, end


def format_summary_markdown(
    region: str,
    year: int,
    month: int,
    rows: list[dict],
    over_cap_ranges: list[tuple[str, str, int, int]] | None = None,
    history: dict | None = None,
) -> str:
    month_label = date(year, month, 1).strftime("%B %Y")
    cap_kwh = PLAN.cap_demand_kw / 12
    lines = [
        f"# {region} energy usage -- {month_label}",
        "",
        "| Date | Usage (kWh) | Avg 5-min (kWh) | Cost ($) | Rate ($/kWh) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    total_usage = total_cost = 0.0
    any_usage = any_cost = False
    interval_avgs = []
    cap_watch = []
    for r in rows:
        usage, cost = r["usage"], r["cost"]
        interval_avg = r.get("interval_avg")
        over_cap = r.get("interval_over_cap")
        usage_s = f"{usage:.3f}" if usage is not None else "-"
        interval_avg_s = f"{interval_avg:.4f}" if interval_avg is not None else "-"
        cost_s = f"{cost:.2f}" if cost is not None else "-"
        rate_s = f"{cost / usage:.4f}" if usage and cost is not None else "-"
        lines.append(f"| {r['date'].strftime('%a %d %b')} | {usage_s} | {interval_avg_s} | {cost_s} | {rate_s} |")
        if usage is not None:
            total_usage += usage
            any_usage = True
        if cost is not None:
            total_cost += cost
            any_cost = True
        if interval_avg is not None:
            interval_avgs.append(interval_avg)
        if over_cap:
            cap_watch.append((r["date"], over_cap))
    total_usage_s = f"{total_usage:.3f}" if any_usage else "-"
    total_cost_s = f"{total_cost:.2f}" if any_cost else "-"
    total_rate_s = f"{total_cost / total_usage:.4f}" if any_usage and any_cost and total_usage else "-"
    month_interval_avg_s = f"{sum(interval_avgs) / len(interval_avgs):.4f}" if interval_avgs else "-"
    lines.append(
        f"| **Total** | **{total_usage_s}** | **{month_interval_avg_s}** | **{total_cost_s}** | **{total_rate_s}** |"
    )
    if not rows:
        lines.append("")
        lines.append("_No saved days this month._")
    lines.append("")
    predicted = predicted_cycle_cost(history, region) if history is not None else None
    if predicted is not None:
        predicted_total, days_with_cost, cycle_days, cyc_start, cyc_end = predicted
        if (cyc_start.year, cyc_start.month) == (year, month) or (cyc_end.year, cyc_end.month) == (year, month):
            avg_cost = predicted_total / cycle_days
            cyc_label = f"{cyc_start.strftime('%d %b')} - {cyc_end.strftime('%d %b')}"
            lines.append(
                f"**Billing cycle ({cyc_label}, {cycle_days} days):** predicted total ${predicted_total:.2f} "
                f"(${avg_cost:.2f}/day avg across {days_with_cost} saved day(s) so far - WholeSave's Billing "
                "Period is a fixed 28 days, not a calendar month)"
            )
            lines.append("")
    if cap_watch:
        lines.append(
            f"**⚠️ Cap watch:** {PLAN.cap_demand_kw:.1f}kW demand cap = {cap_kwh:.4f} kWh/5-min interval. "
            "Exceeding that alone costs nothing unless the AEMO spot price that same interval was *also* "
            f"above ${CAP:.2f}/kWh - below that, min(price, cap) is the same number either way (see "
            "qld_price.Plan.estimate_hourly_cost). Intervals below only count if AEMO data confirms the "
            "spike; missing/unpublished AEMO data for an interval defaults to not counting it. Day(s) with "
            "at least one such interval this month:"
        )
        for d, over_cap in cap_watch:
            lines.append(f"- {d.strftime('%a %d %b')}: {over_cap} interval(s) lost cap protection")

        recurring = [r for r in (over_cap_ranges or []) if r[2] >= 2]
        if recurring:
            lines.append("")
            lines.append(
                "**Common over-cap windows** (same clock-time window lost cap protection - demand over 3kW "
                "*and* AEMO price spiking above the cap - on multiple days; likely a specific appliance or "
                "habit, worth checking against what's running then):"
            )
            for start, end, day_count, total_count in recurring[:15]:
                window = start if start == end else f"{start}-{end}"
                lines.append(f"- {window}: lost cap protection on {day_count} day(s) ({total_count} interval(s) total)")
            lines.append("")
            lines.append(
                "A window at the same clock time every day, with a fairly steady kWh per interval, points at "
                "a timer/thermostat-controlled load rather than variable behaviour - a hot water system is a "
                "common culprit. Under WholeSave, controlled load isn't billed as a separate flat rate - it's "
                "folded into general usage and exposed to the same wholesale/network pricing as everything "
                "else, so a controlled-load window losing the price cap here is a real cost, not just a "
                "modelling artifact."
            )
        elif over_cap_ranges:
            lines.append("")
            lines.append(
                "No clock-time window lost cap protection on more than one day this month - each "
                "cap-losing interval looks like a one-off rather than a recurring pattern."
            )
    else:
        lines.append(
            f"No 5-min interval this month lost the {PLAN.cap_demand_kw:.1f}kW demand cap's protection "
            f"({cap_kwh:.4f} kWh/5-min interval, with AEMO price confirmed above ${CAP:.2f}/kWh) - either "
            "usage stayed under the cap, or the spot price never spiked past the cap when it didn't."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.extend(plan_footnote_lines())
    return "\n".join(lines) + "\n"


def plan_footnote_lines() -> list[str]:
    """Explains what's *not* in the summary table's Cost column (it's the
    billed amount as saved/entered, not derived from these) but does make up
    a typical bill: the plan's fixed daily charges and per-kWh network
    charges by time-of-use period, plus how the wholesale energy charge
    itself (Rate) is derived. Pulls from the module-level PLAN/CAP - the
    account's fixed billing config, same as draw_usage_cost()."""
    p = PLAN
    fixed_daily = p.daily_charge + p.membership_fee + (p.cap_demand_kw * p.cap_fee_per_kw_day)
    lines = [
        "Fixed daily charges (on top of usage):",
        f"- Daily charge: ${p.daily_charge:.3f}/day",
        f"- Membership fee: ${p.membership_fee:.3f}/day",
        f"- Wholesale demand cap fee: {p.cap_demand_kw:.2f} kW @ ${p.cap_fee_per_kw_day:.3f}/kW/day = "
        f"${p.cap_demand_kw * p.cap_fee_per_kw_day:.3f}/day",
    ]
    if p.controlled_load_kwh_per_day:
        lines.append(
            f"- Controlled load: {p.controlled_load_kwh_per_day:.2f} kWh/day @ ${p.controlled_load_rate:.4f}/kWh"
        )
    lines += [
        f"- Total fixed: ${fixed_daily:.3f}/day (${fixed_daily / 24:.4f}/h)",
        "",
        "Network (distribution) charges, per kWh of usage, by time-of-use period:",
        f"- Peak (16:00-21:00): ${p.peak_network_rate:.4f}/kWh",
        f"- Off-peak (09:00-16:00): ${p.offpeak_network_rate:.4f}/kWh",
        f"- Shoulder (21:00-09:00): ${p.shoulder_network_rate:.4f}/kWh",
        "",
        f"Wholesale energy charge: spot price per 5-min interval (capped at ${CAP:.2f}/kWh) x "
        f"{p.dlf:.5f} DLF x {p.mlf:.4f} MLF, + {p.gst_rate * 100:.0f}% GST.",
        "",
        "Cost is the amount actually billed (as saved/entered in the app); Rate is a wholesale-only "
        "figure shown for comparison, not what you were charged.",
        "",
        "Avg 5-min usage comes from that day's real per-interval readings in imported actual data "
        "(blank if the day isn't covered by an import) - a rough continuous-draw indicator "
        "(1 kWh/5min = 12 kW average).",
        "",
        f"Your Wholesale Cap Demand is {p.cap_demand_kw:.1f}kW, i.e. {p.cap_demand_kw / 12:.4f} kWh per "
        f"5-min interval. The price cap (${CAP:.2f}/kWh) only applies to intervals at or under that - go "
        "over it in a single interval and *that interval* is billed at the full uncapped spot price "
        "instead (see qld_price.Plan.estimate_hourly_cost). It's a per-interval instantaneous check, not "
        "a daily or monthly average, so a single short spike (e.g. several appliances running at once) "
        "can trip it even on a low-usage day - see the Cap watch note above.",
    ]
    return lines


def summary_export_path(region: str, year: int, month: int) -> str:
    return os.path.join(os.path.dirname(HISTORY_FILE), f"summary_{region}_{year:04d}-{month:02d}.md")


def load_history() -> dict:
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_history(data: dict) -> None:
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, HISTORY_FILE)


def set_entry(history: dict, region: str, day: date, hour: int | None, fields: dict[str, float | None]) -> None:
    """Merge `fields` into the day/hour bucket: a key set to a float is
    stored, a key set to None is cleared. Keys not present in `fields` at
    all are left untouched (so e.g. saving usage/cost doesn't wipe out a
    peak/off-peak/shoulder breakdown saved earlier for the same day)."""
    region_entry = history.setdefault(region, {})
    day_entry = region_entry.setdefault(day.isoformat(), {})
    if hour is None:
        bucket = day_entry.setdefault("day", {})
    else:
        bucket = day_entry.setdefault("hours", {}).setdefault(str(hour), {})
    for k, v in fields.items():
        if v is None:
            bucket.pop(k, None)
        else:
            bucket[k] = v
    if not bucket:
        if hour is None:
            day_entry.pop("day", None)
        else:
            day_entry["hours"].pop(str(hour), None)
            if not day_entry.get("hours"):
                day_entry.pop("hours", None)
    if not day_entry.get("day") and not day_entry.get("hours"):
        region_entry.pop(day.isoformat(), None)


# ---- app state -----------------------------------------------------------


@dataclass
class Fields:
    usage: str = ""
    cost: str = ""
    day_usage: str = ""
    day_cost: str = ""


class App:
    def __init__(self, stdscr, day: date, region: str):
        self.stdscr = stdscr
        self.day = day
        self.hour: int | None = None
        self.region_idx = REGIONS.index(region) if region in REGIONS else 0
        self.view = "detail"
        self.summary_year, self.summary_month = day.year, day.month
        self.summary_fetch_error = ""
        self.cache: dict[tuple[str, int, int], list[tuple[str, float]]] = {}
        self.history = load_history()
        self.fields = Fields()
        self.status = ""
        self.error = ""
        self.dirty = False
        self.day_rows: list[tuple[str, float]] = []
        self.window_rows: list[tuple[str, float]] = []
        self.window_stamps: set[str] = set()
        self.lookup: dict[str, float] = {}
        self.hour_stats: dict[int, dict] = {}
        self.actual_data: dict[str, tuple[float, float]] = qph.sync_actual_data()
        self.actual_fetch_error: list[str] = []
        self.auto_fetch_actual(self.day)
        self.load_fields_from_history()
        self.fetch_current()
        self.apply_actual_autofill()

    def auto_fetch_actual(self, day: date) -> None:
        """If `day` has no imported actual-data coverage yet, try pulling it
        straight from the GloBird portal API (qld_globird_fetch.py) instead
        of requiring a manual CSV export/drop - mirrors what that script
        does standalone, just triggered from here since the TUI already
        opens on yesterday by default and that's usually the day worth
        having fresh. Only attempted for days that have already finished
        (not today/future, where there's nothing to fetch yet). Any
        failure - most commonly an expired session cookie, but also e.g. a
        disk/permission error saving the export or merging it into the
        actual-data cache - is recorded in self.actual_fetch_error (a list
        of lines: a headline, then any recovery steps the failure came
        with - e.g. qld_globird_fetch's REAUTH_HELP) for draw_detail() to
        show in full, rather than raised (a stale/missing cookie or a
        transient portal issue shouldn't crash the TUI). Every attempt,
        success or failure, is also appended to qgf.LOG_FILE - the on-screen
        error only lives as long as that terminal session, so without a
        persistent log a failure that happens before you're watching (e.g.
        auto-fetch running before GloBird has published yesterday's data
        yet) is otherwise undiagnosable after the fact."""
        if day >= date.today():
            return
        prefix = day.strftime("%Y/%m/%d")
        if any(ts.startswith(prefix) for ts in self.actual_data):
            return
        try:
            cookie = qgf.load_cookie()
            data = qgf.fetch_csv(day, day, cookie)
            qgf.save_export(data, day, day)
            self.actual_data = qph.sync_actual_data()
        except (SystemExit, RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
            headline = f"GloBird auto-fetch for {day.strftime('%d-%m-%Y')} failed:"
            detail_lines = [line for line in str(exc).splitlines() if line.strip()]
            self.actual_fetch_error = [headline, *detail_lines]
            qgf.log_attempt(f"FAILED {day.strftime('%d-%m-%Y')}: {exc}")
            return
        self.actual_fetch_error = []
        self.status = f"Auto-fetched {day.strftime('%d-%m-%Y')} from GloBird."
        qgf.log_attempt(f"OK {day.strftime('%d-%m-%Y')}")

    @property
    def region(self) -> str:
        return REGIONS[self.region_idx]

    # -- data ---------------------------------------------------------

    def fetch_current(self, force: bool = False) -> None:
        self.error = ""
        # Always pull in the next day's month too: hour 23's window (23:05
        # through next-day 00:00) needs it, and so does the per-hour table's
        # hour-23 row, regardless of what's currently selected.
        next_day = self.day + timedelta(days=1)
        months = {(self.day.year, self.day.month), (next_day.year, next_day.month)}
        if force:
            for y, m in months:
                self.cache.pop((self.region, y, m), None)
        try:
            for y, m in months:
                key = (self.region, y, m)
                if key not in self.cache:
                    self.cache[key] = qph.fetch_month(self.region, y, m)
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure in the UI
            self.error = f"Fetch failed: {exc}"
            self.day_rows, self.window_rows, self.window_stamps = [], [], set()
            self.lookup, self.hour_stats = {}, {}
            return

        all_rows: list[tuple[str, float]] = []
        for y, m in months:
            all_rows.extend(self.cache[(self.region, y, m)])
        all_rows = qph.augment_with_live(all_rows, self.region, self.day)
        self.lookup = dict(all_rows)
        self.day_rows = sorted(qph.day_prices(all_rows, self.day))

        if self.hour is not None:
            _, _, stamps = qph.hour_window(self.day, self.hour)
            self.window_stamps = set(stamps)
            self.window_rows = [(ts, self.lookup[ts]) for ts in stamps if ts in self.lookup]
        else:
            self.window_stamps = set()
            self.window_rows = self.day_rows

        self.hour_stats = {}
        for h in range(24):
            _, _, stamps = qph.hour_window(self.day, h)
            rows = [(ts, self.lookup[ts]) for ts in stamps if ts in self.lookup]
            usage, charge, covered = self.actual_totals(stamps)
            entry: dict = {}
            if rows:
                prices = [p / 1000.0 for _, p in rows]
                entry["avg"] = sum(prices) / len(prices)
                entry["n"] = len(rows)
            if covered:
                entry["usage"] = usage
                entry["charge"] = charge
                entry["covered"] = covered
            if entry:
                self.hour_stats[h] = entry

    def actual_totals(self, stamps: list[str]) -> tuple[float, float, int]:
        """Sum real (usage_kwh, charge_$) from imported data for the given
        timestamps. Returns (usage, charge, covered_count)."""
        usage = charge = 0.0
        covered = 0
        for ts in stamps:
            got = self.actual_data.get(ts)
            if got is not None:
                usage += got[0]
                charge += got[1]
                covered += 1
        return usage, charge, covered

    def apply_actual_autofill(self) -> None:
        """If the currently-selected window (whole day or one hour) is fully
        covered by imported actual data and the Usage field is still blank,
        pre-fill it with the real total - a convenience so it (and the
        modelled estimate) reflect reality without having to type anything.
        Doesn't touch a field that already has a value from history or a
        prior edit."""
        if self.fields.usage.strip() or not self.window_rows:
            return
        stamps = [ts for ts, _ in self.window_rows]
        usage, _, covered = self.actual_totals(stamps)
        if covered == len(stamps):
            self.fields.usage = f"{usage:.4f}"
            self.dirty = True

    # -- history --------------------------------------------------------

    def load_fields_from_history(self) -> None:
        entry = self.history.get(self.region, {}).get(self.day.isoformat(), {})
        if self.hour is None:
            d = entry.get("day", {})
            self.fields = Fields(
                usage=fmt_opt(d.get("usage")),
                cost=fmt_opt(d.get("cost")),
            )
        else:
            h = entry.get("hours", {}).get(str(self.hour), {})
            d = entry.get("day", {})
            self.fields = Fields(
                usage=fmt_opt(h.get("usage")),
                cost=fmt_opt(h.get("cost")),
                day_usage=fmt_opt(d.get("usage")),
                day_cost=fmt_opt(d.get("cost")),
            )
        self.dirty = False

    def has_entry(self, day: date | None = None) -> bool:
        day = day or self.day
        entry = self.history.get(self.region, {}).get(day.isoformat(), {})
        return bool(entry.get("day") or entry.get("hours"))

    def saved_dates(self) -> list[date]:
        out = []
        for key in self.history.get(self.region, {}):
            try:
                out.append(datetime.strptime(key, "%Y-%m-%d").date())
            except ValueError:
                continue
        return sorted(out)

    def save(self) -> None:
        if self.hour is None:
            set_entry(self.history, self.region, self.day, None, {
                "usage": parse_float(self.fields.usage),
                "cost": parse_float(self.fields.cost),
            })
        else:
            set_entry(self.history, self.region, self.day, self.hour, {
                "usage": parse_float(self.fields.usage),
                "cost": parse_float(self.fields.cost),
            })
            set_entry(self.history, self.region, self.day, None, {
                "usage": parse_float(self.fields.day_usage),
                "cost": parse_float(self.fields.day_cost),
            })
        save_history(self.history)
        self.dirty = False
        self.status = "Saved."

    # -- navigation -----------------------------------------------------

    def on_context_changed(self) -> None:
        self.fetch_current()
        self.load_fields_from_history()
        self.apply_actual_autofill()
        self.status = ""

    def change_day(self, delta: int) -> None:
        self.day += timedelta(days=delta)
        self.on_context_changed()

    def change_hour(self, delta: int) -> None:
        self.hour = 0 if self.hour is None else (self.hour + delta) % 24
        self.on_context_changed()

    def all_day(self) -> None:
        self.hour = None
        self.on_context_changed()

    def cycle_region(self) -> None:
        self.region_idx = (self.region_idx + 1) % len(REGIONS)
        self.on_context_changed()

    def jump_saved(self, direction: int) -> None:
        dates = self.saved_dates()
        candidates = [d for d in dates if (d < self.day if direction < 0 else d > self.day)]
        if not candidates:
            self.status = "No earlier saved days." if direction < 0 else "No later saved days."
            return
        self.day = candidates[-1] if direction < 0 else candidates[0]
        self.on_context_changed()

    # -- month summary page ----------------------------------------------

    def load_summary_prices(self, force: bool = False) -> None:
        """Fetch (or reuse from the shared month cache) the AEMO spot price
        rows for the summary page's region/month - used to confirm actual
        price spikes for the Cap watch feature (see interval_prices_from_rows()
        / _interval_lost_cap()). Failures are recorded in
        self.summary_fetch_error rather than raised, since day usage/cost
        (the summary's main content) doesn't depend on this succeeding."""
        key = (self.region, self.summary_year, self.summary_month)
        if force:
            self.cache.pop(key, None)
        try:
            if key not in self.cache:
                self.cache[key] = qph.fetch_month(*key)
            self.summary_fetch_error = ""
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure in the UI
            self.summary_fetch_error = f"Spot price fetch failed: {exc}"

    def change_summary_month(self, delta: int) -> None:
        m = self.summary_month - 1 + delta
        self.summary_year += m // 12
        self.summary_month = m % 12 + 1
        self.status = ""
        self.load_summary_prices()

    def export_summary(self) -> None:
        price_rows = self.cache.get((self.region, self.summary_year, self.summary_month))
        interval_prices = interval_prices_from_rows(price_rows) if price_rows is not None else None
        interval_stats = day_interval_stats(
            self.actual_data, self.summary_year, self.summary_month, interval_prices
        )
        rows = month_summary_rows(
            self.history, self.region, self.summary_year, self.summary_month, interval_stats
        )
        over_cap_ranges = month_over_cap_ranges(
            self.actual_data, self.summary_year, self.summary_month, interval_prices
        )
        md = format_summary_markdown(
            self.region, self.summary_year, self.summary_month, rows, over_cap_ranges, self.history
        )
        path = summary_export_path(self.region, self.summary_year, self.summary_month)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(md)
        self.status = f"Exported {len(rows)} day(s) to {path}"

    # -- editing ----------------------------------------------------------

    def edit_field(self, prompt: str, initial: str) -> str | None:
        """Enter confirms the typed value (s still saves it to disk), Esc
        cancels (keeps the old value), Ctrl-U clears the field in one go
        so a mistyped value can just be retyped."""
        win = self.stdscr
        h, w = win.getmaxyx()
        y = h - 1
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                win.move(y, 0)
                win.clrtoeol()
                text = (prompt + "".join(buf) + "  [Enter=OK Esc=cancel ^U=clear]")[: max(w - 1, 0)]
                win.addstr(y, 0, text)
                win.move(y, min(len(prompt) + len(buf), max(w - 1, 0)))
                win.refresh()
                ch = win.getch()
                if ch in (curses.KEY_ENTER, 10, 13):
                    return "".join(buf)
                if ch == 27:  # Esc
                    return None
                if ch == 21:  # Ctrl-U: clear the field
                    buf = []
                elif ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    c = chr(ch)
                    if c.isdigit() or c in ".-":
                        buf.append(c)
        finally:
            curses.curs_set(0)

    def handle_summary_key(self, ch: int) -> bool:
        """Return False to quit."""
        if ch == ord("q"):
            return False
        elif ch in (27, ord("m")):
            self.view = "detail"
        elif ch == curses.KEY_LEFT:
            self.change_summary_month(-1)
        elif ch == curses.KEY_RIGHT:
            self.change_summary_month(1)
        elif ch == ord("g"):
            self.cycle_region()
            self.load_summary_prices()
        elif ch == ord("r"):
            self.load_summary_prices(force=True)
            self.status = "Refreshed spot prices."
        elif ch == ord("e"):
            self.export_summary()
        return True

    def handle_key(self, ch: int) -> bool:
        """Return False to quit."""
        if self.view == "summary":
            return self.handle_summary_key(ch)
        if ch in (ord("q"), 27):
            return False
        elif ch == ord("m"):
            self.view = "summary"
            self.summary_year, self.summary_month = self.day.year, self.day.month
            self.load_summary_prices()
        elif ch == curses.KEY_LEFT:
            self.change_day(-1)
        elif ch == curses.KEY_RIGHT:
            self.change_day(1)
        elif ch == curses.KEY_UP:
            self.change_hour(-1)
        elif ch == curses.KEY_DOWN:
            self.change_hour(1)
        elif ch == ord("a"):
            self.all_day()
        elif ch == ord("g"):
            self.cycle_region()
        elif ch == ord("["):
            self.jump_saved(-1)
        elif ch == ord("]"):
            self.jump_saved(1)
        elif ch == ord("r"):
            self.fetch_current(force=True)
            before = len(self.actual_data)
            self.actual_data = qph.sync_actual_data()
            self.auto_fetch_actual(self.day)
            self.apply_actual_autofill()
            new = len(self.actual_data) - before
            self.status = f"Refreshed.{f' Imported {new} new actual intervals.' if new > 0 else ''}"
        elif ch == ord("s"):
            self.save()
        elif ch == ord("u"):
            val = self.edit_field("Window usage (kWh): ", self.fields.usage)
            if val is not None:
                self.fields.usage, self.dirty = val, True
        elif ch == ord("c"):
            val = self.edit_field("Window cost ($): ", self.fields.cost)
            if val is not None:
                self.fields.cost, self.dirty = val, True
        elif ch == ord("U"):
            if self.hour is None:
                self.status = "Day totals only apply once you pick an hour (↑/↓)."
            else:
                val = self.edit_field("Whole day usage (kWh): ", self.fields.day_usage)
                if val is not None:
                    self.fields.day_usage, self.dirty = val, True
        elif ch == ord("C"):
            if self.hour is None:
                self.status = "Day totals only apply once you pick an hour (↑/↓)."
            else:
                val = self.edit_field("Whole day cost ($): ", self.fields.day_cost)
                if val is not None:
                    self.fields.day_cost, self.dirty = val, True
        return True

    # -- drawing ----------------------------------------------------------

    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.stdscr.addnstr(y, x, text, max(w - x - 1, 0), attr)
        except curses.error:
            pass

    def draw_chart(self, y: int, w: int) -> int:
        """Draws the day's price sparkline at row y, returns next free row.
        Buckets by actual clock time (minutes since midnight), not by
        sequential position in self.day_rows - on an in-progress day (fewer
        rows than a full 288-interval day), bucketing by sequential index
        would stretch however little data exists across the *entire* chart
        width, making a handful of morning hours look like a complete day
        ending at 24:00. Bucketing by real time instead leaves the
        not-yet-happened portion of the day blank, and the 00:00-24:00 axis
        labels (based on the fixed chart width, not on how much data
        exists) stay honest about where "now" actually falls."""
        if not self.day_rows:
            self.safe_addstr(y, 0, "(no price data for this day yet)")
            return y + 2

        width = max(w - 2, 1)
        minutes_per_day = 24 * 60
        buckets: list[list[tuple[str, float]]] = [[] for _ in range(width)]
        for ts, price in self.day_rows:
            minutes = int(ts[11:13]) * 60 + int(ts[14:16])
            x = min(width - 1, minutes * width // minutes_per_day)
            buckets[x].append((ts, price))

        day_prices = [p / 1000.0 for _, p in self.day_rows]
        day_avg = sum(day_prices) / len(day_prices)
        lo, hi = min(day_prices), max(day_prices)
        spread = (hi - lo) or 1.0

        for x, b in enumerate(buckets):
            if not b:
                continue
            avg = sum(p / 1000.0 for _, p in b) / len(b)
            level = min(len(SPARK) - 1, max(0, round((avg - lo) / spread * (len(SPARK) - 1))))
            spike = avg > day_avg * 1.5
            selected = any(ts in self.window_stamps for ts, _ in b)
            attr = curses.color_pair(2) if spike else curses.color_pair(1)
            if selected:
                attr |= curses.A_REVERSE
            self.safe_addstr(y, x, SPARK[level], attr)

        # axis labels roughly under 00:00 / 06:00 / 12:00 / 18:00 / 24:00
        for frac, label in ((0.0, "00:00"), (0.25, "06:00"), (0.5, "12:00"), (0.75, "18:00"), (1.0, "24:00")):
            lx = min(int(frac * (width - 1)), max(width - len(label), 0))
            self.safe_addstr(y + 1, lx, label, curses.A_DIM)
        return y + 2

    def draw_hour_table(self, y: int, w: int) -> int:
        """Draws a one-row-per-hour table (Hour / Avg spot / Usage / Cost) at
        row y, returns next free row. Deliberately vertical (24 rows) rather
        than the old compact multi-column grid - easier to read than cramming
        avg+usage+cost into a small cell. Hours covered by imported actual
        data show real usage/cost (self.hour_stats' "usage"/"charge"/
        "covered" - see fetch_current()) alongside the AEMO spot-price
        average; hours without an import show spot-price-average only, with
        "--" for usage/cost. A trailing "~" on a value marks partial hour
        coverage (<12 of the hour's 5-min intervals present)."""
        if not self.hour_stats:
            self.safe_addstr(y, 0, "(no hourly data for this day yet)")
            return y + 1

        day_vals = [p / 1000.0 for _, p in self.day_rows] if self.day_rows else []
        day_avg = sum(day_vals) / len(day_vals) if day_vals else 0.0

        col = "{:<13}{:>16}{:>13}{:>10}"
        self.safe_addstr(y, 0, col.format("Hour", "Avg spot ($/kWh)", "Usage (kWh)", "Cost ($)"), curses.A_BOLD)
        y += 1
        rule = "-" * min(w - 1, 52)
        self.safe_addstr(y, 0, rule)
        y += 1

        max_y = self.stdscr.getmaxyx()[0] - 1
        for h in range(24):
            if y >= max_y:
                self.safe_addstr(y, 0, f"... {24 - h} more hour(s) not shown (resize taller to see all)", curses.A_DIM)
                return y + 1
            label = f"{h:02d}:00-{(h + 1) % 24:02d}:00"
            period_attr = curses.color_pair(TOU_COLOR_PAIR[qp.tou_period_for_hour(h)])
            stats = self.hour_stats.get(h)
            if stats is None:
                self.safe_addstr(y, 0, col.format(label, "--", "--", "--"), period_attr | curses.A_DIM)
                y += 1
                continue
            avg = stats.get("avg")
            usage, charge, covered = stats.get("usage"), stats.get("charge"), stats.get("covered")
            avg_s = f"${avg:.4f}{'~' if stats.get('n', 0) < 12 else ''}" if avg is not None else "--"
            if usage is not None:
                mark = "~" if covered < 12 else ""
                usage_s, cost_s = f"{usage:.2f}{mark}", f"${charge:.2f}"
            else:
                usage_s = cost_s = "--"
            spike = day_avg and avg is not None and avg > day_avg * 1.5
            attr = period_attr | (curses.A_BOLD if spike else 0)
            if self.hour == h:
                attr |= curses.A_REVERSE | curses.A_BOLD
            self.safe_addstr(y, 0, col.format(label, avg_s, usage_s, cost_s), attr)
            y += 1
        return y

    def draw_usage_cost(self, y: int, w: int) -> tuple[int, float | None]:
        """Actual (imported) vs manual estimate, side by side as an aligned
        table - replaces the old free-text lines (a prose "draw_actual()"
        block plus a separate inline manual-entry block), which were hard
        to scan/compare. "-" marks a value that isn't available (no import
        coverage for Actual, or the u/c manual fields still unset for
        Estimate).

        Actual's usage/energy charge is the retailer's own already-computed
        per-interval Wholesale Usage Charge (no flat-load modelling), plus
        the network charge computed exactly per interval from the real data
        - shown as one row per TOU meter the window touches (peak/off-peak/
        shoulder, qph.actual_network_charge_by_period() - colored to match
        draw_hour_table()'s per-hour coloring), rather than a single lumped
        figure. Controlled load is NOT added separately: T&Cs paragraph 5
        defines the billed usage as including "any controlled load, if
        applicable", and this export's Usage figure already reflects that -
        confirmed empirically (adding a flat controlled-load charge on top
        overshot two real bills by ~$0.52; without it they're within ~1.5%,
        i.e. rounding).

        The Estimate column's per-meter Network rows and Avg draw are
        themselves estimates, not real data - qph.estimated_network_charge_
        by_period() and the avg-draw figure both assume the Estimate's
        usage is spread evenly across the window (the same flat-load
        assumption qph.estimate_cost() already uses for the wholesale
        energy charge), marked with "~" to flag that. Less trustworthy than
        Actual's whenever real usage isn't actually flat (e.g. a
        controlled-load spike concentrated in one TOU period) - Actual is
        the reliable figure whenever it's available.

        If nothing's been typed into the manual Usage field yet but Actual
        has *some* coverage (typically an in-progress "today", which
        apply_actual_autofill() deliberately won't autofill until the whole
        window is covered - see its docstring, and the bug that motivated
        that guard), the Estimate column falls back to using that partial
        actual usage as its basis instead of sitting entirely blank -
        marked with "*". This never touches self.fields.usage itself (so
        pressing s to save still only saves what was actually typed, not a
        partial-day figure masquerading as a final one) - it's display-only.

        Returns (next free row, e_total) - e_total (the manual estimate's
        total, or None if there's no usage to derive one from) is handed
        back so draw_detail() can reuse it for the whole-day scaling
        section below, without re-running qph.estimate_cost() a second
        time."""
        if not self.window_rows:
            return y, None

        cost = parse_float(self.fields.cost)
        stamps = [ts for ts, _ in self.window_rows]
        a_usage, a_energy, covered = self.actual_totals(stamps)
        have_actual = covered > 0

        values = [p / 1000.0 for _, p in self.window_rows]
        hours = len(self.window_rows) * (5 / 60)

        a_avg = a_fixed = a_total = None
        a_network_by_period: dict[str, float] = {}
        if have_actual:
            stamped_usage = [(ts, self.actual_data[ts][0]) for ts in stamps if ts in self.actual_data]
            a_network_by_period = qph.actual_network_charge_by_period(stamped_usage, PLAN)
            a_network = sum(a_network_by_period.values())
            core_fixed_per_day = PLAN.daily_charge + PLAN.membership_fee + (PLAN.cap_demand_kw * PLAN.cap_fee_per_kw_day)
            a_fixed = core_fixed_per_day * hours / 24
            a_total = a_fixed + a_energy + a_network
            a_avg = a_usage / covered

        e_usage = parse_float(self.fields.usage)
        e_from_actual = e_usage is None and have_actual
        e_usage_basis = a_usage if e_from_actual else e_usage
        e_fixed = e_energy = e_network = e_total = e_avg = None
        e_network_by_period: dict[str, float] = {}
        if e_usage_basis is not None:
            _e_total, e_fixed, e_energy, _capped = qph.estimate_cost(values, e_usage_basis, PLAN, CAP, hours)
            e_network_by_period = qph.estimated_network_charge_by_period(stamps, e_usage_basis, PLAN)
            e_network = sum(e_network_by_period.values())
            e_total = e_fixed + e_energy + e_network
            e_avg = e_usage_basis / len(stamps) if stamps else None

        # Which TOU meters this window actually touches, peak/off-peak/
        # shoulder order - just one for a single-hour window, up to all
        # three for the whole day (see qp.tou_period_for_hour()).
        present_periods = {qp.tou_period_for_hour(qph.interval_hour(ts)) for ts in stamps}
        periods_present = [p for p in ("peak", "offpeak", "shoulder") if p in present_periods]
        period_label = {
            p: f"{name} {qp.network_rate_for_period(p, PLAN):.4f}"
            for p, name in (("peak", "peak"), ("offpeak", "off-peak"), ("shoulder", "shoulder"))
        }

        label_w, col_w = 26, 16

        def cell(v: float | None, fmt: str = "${:.2f}", suffix: str = "") -> str:
            return "-" if v is None else fmt.format(v) + suffix

        def row(
            label: str, a: float | None, e: float | None, fmt: str = "${:.2f}",
            a_suffix: str = "", e_suffix: str = "",
        ) -> str:
            return f"{label:<{label_w}}{cell(a, fmt, a_suffix):>{col_w}}{cell(e, fmt, e_suffix):>{col_w}}"

        if have_actual and covered != len(stamps):
            self.safe_addstr(y, 0, f"(Actual data covers {covered}/{len(stamps)} intervals for this window)", curses.A_DIM)
            y += 1
        header = f"{'':<{label_w}}{'Actual':>{col_w}}{'Estimate':>{col_w}}"
        self.safe_addstr(y, 0, header, curses.A_BOLD)
        y += 1
        rule = "-" * min(w - 1, label_w + col_w * 2)
        self.safe_addstr(y, 0, rule)
        y += 1

        self.safe_addstr(
            y, 0,
            row(
                "Usage (kWh) [u]", a_usage if have_actual else None, e_usage_basis, "{:.3f}",
                e_suffix="*" if e_from_actual else "",
            ),
        )
        y += 1
        self.safe_addstr(
            y, 0, row("Avg draw (kWh/5min)", a_avg, e_avg, "{:.4f}", e_suffix="~" if e_avg is not None else "")
        )
        y += 1
        self.safe_addstr(y, 0, row("Fixed charge", a_fixed, e_fixed))
        y += 1
        self.safe_addstr(y, 0, row("Energy charge", a_energy if have_actual else None, e_energy))
        y += 1
        for period in periods_present:
            a_val = a_network_by_period.get(period)
            e_val = e_network_by_period.get(period)
            self.safe_addstr(
                y, 0,
                row(f"Network ({period_label[period]})", a_val, e_val, e_suffix="~" if e_val is not None else ""),
                curses.color_pair(TOU_COLOR_PAIR[period]),
            )
            y += 1
        self.safe_addstr(y, 0, rule)
        y += 1
        self.safe_addstr(y, 0, row("Total", a_total, e_total), curses.A_BOLD)
        y += 1

        if cost is not None and (a_total is not None or e_total is not None):
            a_diff = cost - a_total if a_total is not None else None
            e_diff = cost - e_total if e_total is not None else None
            self.safe_addstr(
                y, 0, row(f"vs billed [c] ${cost:.2f}", a_diff, e_diff, "{:+.2f}"), curses.color_pair(3)
            )
            y += 1
        if e_avg is not None or e_network is not None:
            self.safe_addstr(
                y, 0, "  ~ = estimate assumes usage spread evenly across the window", curses.A_DIM
            )
            y += 1
        if e_from_actual:
            self.safe_addstr(
                y, 0,
                "  * Estimate uses partial actual usage so far (not saved) - press u to enter your own",
                curses.A_DIM,
            )
            y += 1
        if e_usage_basis is None and not have_actual:
            self.safe_addstr(y, 0, "No data yet - press u to enter usage manually.", curses.A_DIM)
            y += 1
        return y + 1, e_total

    def draw_summary(self) -> None:
        h, w = self.stdscr.getmaxyx()
        price_rows = self.cache.get((self.region, self.summary_year, self.summary_month))
        interval_prices = interval_prices_from_rows(price_rows) if price_rows is not None else None
        interval_stats = day_interval_stats(
            self.actual_data, self.summary_year, self.summary_month, interval_prices
        )
        rows = month_summary_rows(
            self.history, self.region, self.summary_year, self.summary_month, interval_stats
        )
        over_cap_ranges = month_over_cap_ranges(
            self.actual_data, self.summary_year, self.summary_month, interval_prices
        )
        month_label = date(self.summary_year, self.summary_month, 1).strftime("%B %Y")
        header = f" {self.region}  {month_label}  [MONTH SUMMARY] "
        self.safe_addstr(0, 0, header.ljust(w), curses.A_REVERSE | curses.A_BOLD)

        y = 2
        if self.summary_fetch_error:
            self.safe_addstr(y, 0, self.summary_fetch_error, curses.color_pair(2))
            y += 1

        col = "{:<12}{:>13}{:>13}{:>10}{:>13}"
        rule = "-" * min(w - 1, 63)
        if not rows:
            self.safe_addstr(y, 0, "No saved days this month.", curses.A_DIM)
        else:
            self.safe_addstr(
                y, 0,
                col.format("Date", "Usage (kWh)", "Avg 5m (kWh)", "Cost ($)", "Rate ($/kWh)"),
                curses.A_BOLD,
            )
            y += 1
            self.safe_addstr(y, 0, rule)
            y += 1

            max_rows = max(h - 14 - y, 0)  # leave room for the totals/predicted rows, footnote, status and help lines
            shown = rows[:max_rows]
            for r in shown:
                usage, cost = r["usage"], r["cost"]
                interval_avg_val = r.get("interval_avg")
                usage_s = f"{usage:.3f}" if usage is not None else "-"
                interval_avg_s = f"{interval_avg_val:.4f}" if interval_avg_val is not None else "-"
                cost_s = f"{cost:.2f}" if cost is not None else "-"
                rate_s = f"{cost / usage:.4f}" if usage and cost is not None else "-"
                self.safe_addstr(
                    y, 0,
                    col.format(r["date"].strftime("%a %d %b"), usage_s, interval_avg_s, cost_s, rate_s),
                )
                y += 1
            if len(rows) > len(shown):
                self.safe_addstr(y, 0, f"... and {len(rows) - len(shown)} more day(s) not shown (export to see all)", curses.A_DIM)
                y += 1

            total_usage = sum(r["usage"] for r in rows if r["usage"] is not None)
            total_cost = sum(r["cost"] for r in rows if r["cost"] is not None)
            any_usage = any(r["usage"] is not None for r in rows)
            any_cost = any(r["cost"] is not None for r in rows)
            interval_avgs = [r["interval_avg"] for r in rows if r.get("interval_avg") is not None]
            total_usage_s = f"{total_usage:.3f}" if any_usage else "-"
            total_cost_s = f"{total_cost:.2f}" if any_cost else "-"
            total_rate_s = f"{total_cost / total_usage:.4f}" if any_usage and any_cost and total_usage else "-"
            month_interval_avg_s = f"{sum(interval_avgs) / len(interval_avgs):.4f}" if interval_avgs else "-"
            over_cap_days = [r for r in rows if r.get("interval_over_cap")]
            self.safe_addstr(y, 0, rule)
            y += 1
            self.safe_addstr(
                y, 0,
                col.format("Total", total_usage_s, month_interval_avg_s, total_cost_s, total_rate_s),
                curses.A_BOLD,
            )
            y += 2

            p = PLAN
            fixed_daily = p.daily_charge + p.membership_fee + (p.cap_demand_kw * p.cap_fee_per_kw_day)
            cap_kwh = p.cap_demand_kw / 12

            self.safe_addstr(y, 0, "Plan rates", curses.A_BOLD)
            y += 1
            self.safe_addstr(
                y, 0,
                f"  fixed ${fixed_daily:.3f}/day   peak ${p.peak_network_rate:.4f}/kWh   "
                f"off-peak ${p.offpeak_network_rate:.4f}/kWh   shoulder ${p.shoulder_network_rate:.4f}/kWh",
                curses.A_DIM,
            )
            y += 2

            predicted = predicted_cycle_cost(self.history, self.region)
            show_predicted = predicted is not None and (
                (predicted[3].year, predicted[3].month) == (self.summary_year, self.summary_month)
                or (predicted[4].year, predicted[4].month) == (self.summary_year, self.summary_month)
            )
            if show_predicted:
                predicted_total, days_with_cost, cycle_days, cyc_start, cyc_end = predicted
                avg_cost = predicted_total / cycle_days
                cyc_label = f"{cyc_start.strftime('%d %b')}-{cyc_end.strftime('%d %b')}"
                self.safe_addstr(y, 0, f"Billing cycle ({cyc_label}, {cycle_days} days)", curses.A_BOLD)
                y += 1
                self.safe_addstr(
                    y, 0,
                    f"  predicted total ${predicted_total:.2f}  (${avg_cost:.2f}/day avg x "
                    f"{days_with_cost} saved day(s) so far)",
                    curses.A_DIM,
                )
                y += 2

            self.safe_addstr(y, 0, "Cap watch", curses.A_BOLD)
            y += 1
            cap_line = (
                f"⚠ {len(over_cap_days)} day(s) lost cap protection (>{cap_kwh:.4f} kWh/5min *and* AEMO price spiked)"
                if over_cap_days
                else f"No day lost the {p.cap_demand_kw:.1f}kW demand cap's protection this month (AEMO-confirmed)"
            )
            self.safe_addstr(y, 0, f"  {cap_line}", curses.color_pair(2) if over_cap_days else curses.A_DIM)
            y += 1
            recurring = [r for r in over_cap_ranges if r[2] >= 2]
            if over_cap_days:
                if recurring:
                    top_start, top_end, top_days, top_total = recurring[0]
                    top_window = top_start if top_start == top_end else f"{top_start}-{top_end}"
                    self.safe_addstr(
                        y, 0,
                        f"  most common: {top_window} on {top_days} day(s) ({top_total}x) - see export for full list",
                        curses.A_DIM,
                    )
                    y += 1
                    self.safe_addstr(
                        y, 0,
                        "  same time + steady kWh daily -> likely a timer/thermostat load (hot water?)",
                        curses.A_DIM,
                    )
                    y += 1
                else:
                    self.safe_addstr(
                        y, 0, "  no clock-time window lost cap protection on more than one day (each a one-off)",
                        curses.A_DIM,
                    )
                    y += 1
            y += 1

            self.safe_addstr(y, 0, "Cost is billed, not derived from these figures - full breakdown in export (e)", curses.A_DIM)
            y += 1

        status_line = self.status
        if status_line:
            self.safe_addstr(h - 3, 0, status_line, curses.color_pair(4))
        help_text = "←→ month  g region  r refresh spot prices  e export markdown  m/Esc back to day view  q quit"
        self.safe_addstr(h - 1, 0, help_text[: w - 1], curses.A_DIM)

    def draw_detail(self) -> None:
        h, w = self.stdscr.getmaxyx()

        mode = "ALL DAY" if self.hour is None else f"{self.hour:02d}:00-{(self.hour + 1) % 24:02d}:00"
        header = f" {self.region}  {self.day.strftime('%d-%m-%Y')}  [{mode}] "
        self.safe_addstr(0, 0, header.ljust(w), curses.A_REVERSE | curses.A_BOLD)

        saved = self.has_entry()
        if self.dirty:
            self.safe_addstr(1, 0, "○ unsaved changes (s to save)", curses.color_pair(3))
        elif saved:
            self.safe_addstr(1, 0, "● saved", curses.color_pair(4))

        y = 3
        if self.actual_fetch_error:
            wrap_width = max(w - 1, 20)
            wrapped: list[tuple[str, int]] = []
            for i, line in enumerate(self.actual_fetch_error):
                style = curses.color_pair(2) | curses.A_BOLD if i == 0 else curses.A_DIM
                for wline in textwrap.wrap(line, wrap_width) or [""]:
                    wrapped.append((wline, style))
            max_lines = 10
            for wline, style in wrapped[:max_lines]:
                self.safe_addstr(y, 0, wline, style)
                y += 1
            if len(wrapped) > max_lines:
                self.safe_addstr(y, 0, f"... ({len(wrapped) - max_lines} more line(s))", curses.A_DIM)
                y += 1
            y += 1
        if self.error:
            self.safe_addstr(y, 0, self.error, curses.color_pair(2) | curses.A_BOLD)
            y += 2
        elif not self.window_rows:
            self.safe_addstr(y, 0, "No price data for this window (future date, or not yet published).")
            y += 2
        else:
            values = [p / 1000.0 for _, p in self.window_rows]
            avg, lo, hi = sum(values) / len(values), min(values), max(values)
            peak_t, peak_p = max(self.window_rows, key=lambda r: r[1])
            self.safe_addstr(
                y, 0,
                f"Window avg ${avg:.4f}/kWh   range ${lo:.4f}-${hi:.4f}   peak {peak_t.split(' ')[1]} ${peak_p/1000:.4f}/kWh",
            )
            y += 1
            if self.hour is not None and self.day_rows:
                day_vals = [p / 1000.0 for _, p in self.day_rows]
                day_avg = sum(day_vals) / len(day_vals)
                ratio = avg / day_avg if day_avg else 0.0
                self.safe_addstr(y, 0, f"Day avg    ${day_avg:.4f}/kWh   (this window is {ratio:.1f}x the day average)")
                y += 1
        y += 1

        self.safe_addstr(y, 0, "Day price chart (5-min spot; red = >1.5x day avg, reverse = selected window)", curses.A_DIM)
        y = self.draw_chart(y + 1, w)
        y += 1

        y = self.draw_hour_table(y, w)
        y += 1

        y, total = self.draw_usage_cost(y, w)

        if self.window_rows:
            if self.hour is not None:
                self.safe_addstr(
                    y, 0,
                    f"Whole-day usage [U]: {self.fields.day_usage or '(unset)':>10} kWh    Whole-day cost [C]: {self.fields.day_cost or '(unset)':>10} $",
                )
                y += 1
                day_usage = parse_float(self.fields.day_usage)
                if day_usage is not None and self.day_rows and total is not None:
                    day_values = [p / 1000.0 for _, p in self.day_rows]
                    day_hours = len(self.day_rows) * (5 / 60)
                    day_total, _, _, _ = qph.estimate_cost(day_values, day_usage, PLAN, CAP, day_hours)
                    share = total / day_total if day_total else 0.0
                    self.safe_addstr(y, 0, f"  day estimate ${day_total:.2f} -> this window is ~{share*100:.1f}% of it")
                    y += 1
                    day_cost = parse_float(self.fields.day_cost)
                    if day_cost is not None:
                        self.safe_addstr(y, 0, f"  scaled to actual daily bill ${day_cost:.2f}: this window ≈ ${share*day_cost:.2f}", curses.A_BOLD)
                        y += 1
            y += 1

        status_line = self.status
        if status_line:
            self.safe_addstr(h - 3, 0, status_line, curses.color_pair(4))

        help_text = (
            "←→ day  ↑↓ hour  a all-day  u/c usage/cost  U/C day totals  "
            f"s save  [ ] saved-days  g region({self.region})  r refresh  m summary  q quit"
        )
        self.safe_addstr(h - 1, 0, help_text[: w - 1], curses.A_DIM)

    def draw(self) -> None:
        self.stdscr.erase()
        if self.view == "summary":
            self.draw_summary()
        else:
            self.draw_detail()
        self.stdscr.refresh()

    def run(self) -> None:
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(TOU_COLOR_PAIR["peak"], curses.COLOR_RED, -1)
        curses.init_pair(TOU_COLOR_PAIR["offpeak"], curses.COLOR_GREEN, -1)
        curses.init_pair(TOU_COLOR_PAIR["shoulder"], curses.COLOR_BLUE, -1)
        self.stdscr.keypad(True)
        while True:
            self.draw()
            ch = self.stdscr.getch()
            if not self.handle_key(ch):
                break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("day", nargs="?", help="Date to open on, DD-MM-YYYY (default: yesterday)")
    parser.add_argument("--region", default="QLD1", choices=REGIONS, help="NEM region to start on (default: %(default)s)")
    args = parser.parse_args()

    try:
        day = qph.parse_day(args.day) if args.day else date.today() - timedelta(days=1)
    except ValueError as exc:
        parser.error(str(exc))

    curses.wrapper(lambda stdscr: App(stdscr, day, args.region).run())


if __name__ == "__main__":
    main()
