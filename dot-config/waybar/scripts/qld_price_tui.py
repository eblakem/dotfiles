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
usage/cost plus that day's average AEMO wholesale spot price, for the
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import qld_price as qp
import qld_price_history as qph

REGIONS = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]

HISTORY_FILE = os.path.expanduser(
    os.environ.get(
        "QLD_HISTORY_FILE", "~/braindump/personal/wholesave-globird/qld_price_history_log.json"
    )
)

SPARK = " ▁▂▃▄▅▆▇█"

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


def chunk(seq: list, n: int) -> list[list]:
    """Split seq into n roughly-equal contiguous chunks (n clamped to len(seq))."""
    if not seq:
        return []
    n = max(1, min(n, len(seq)))
    k, m = divmod(len(seq), n)
    chunks, i = [], 0
    for j in range(n):
        size = k + (1 if j < m else 0)
        chunks.append(seq[i : i + size])
        i += size
    return chunks


def month_days(year: int, month: int) -> list[date]:
    first = date(year, month, 1)
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    days, d = [], first
    while d < next_first:
        days.append(d)
        d += timedelta(days=1)
    return days


def day_avgs(rows: list[tuple[str, float]]) -> dict[date, float]:
    """Average spot price ($/kWh) per calendar day, from a list of
    (SETTLEMENTDATE, RRP $/MWh) rows such as qld_price_history.fetch_month()
    returns."""
    buckets: dict[date, list[float]] = {}
    for ts, price in rows:
        d = datetime.strptime(ts[:10], "%Y/%m/%d").date()
        buckets.setdefault(d, []).append(price / 1000.0)
    return {d: sum(prices) / len(prices) for d, prices in buckets.items()}


def month_summary_rows(
    history: dict, region: str, year: int, month: int, day_avg: dict[date, float] | None = None
) -> list[dict]:
    """Whole-day usage/cost for each saved day in `year`/`month`, pulled from
    the "day" bucket (see set_entry()/App.save() - populated either by
    saving in all-day view, or via the U/C whole-day fields from an hour
    view). Days with neither saved are skipped. `day_avg` (from day_avgs())
    fills in each row's average wholesale spot price, when available."""
    region_entry = history.get(region, {})
    day_avg = day_avg or {}
    rows = []
    for d in month_days(year, month):
        day_bucket = region_entry.get(d.isoformat(), {}).get("day", {})
        usage, cost = day_bucket.get("usage"), day_bucket.get("cost")
        if usage is None and cost is None:
            continue
        rows.append({"date": d, "usage": usage, "cost": cost, "avg": day_avg.get(d)})
    return rows


def format_summary_markdown(region: str, year: int, month: int, rows: list[dict]) -> str:
    month_label = date(year, month, 1).strftime("%B %Y")
    lines = [
        f"# {region} energy usage -- {month_label}",
        "",
        "| Date | Usage (kWh) | Cost ($) | Rate ($/kWh) | Day avg spot ($/kWh) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    total_usage = total_cost = 0.0
    any_usage = any_cost = False
    avgs = []
    for r in rows:
        usage, cost, avg = r["usage"], r["cost"], r.get("avg")
        usage_s = f"{usage:.3f}" if usage is not None else "-"
        cost_s = f"{cost:.2f}" if cost is not None else "-"
        rate_s = f"{cost / usage:.4f}" if usage and cost is not None else "-"
        avg_s = f"{avg:.4f}" if avg is not None else "-"
        lines.append(f"| {r['date'].strftime('%a %d %b')} | {usage_s} | {cost_s} | {rate_s} | {avg_s} |")
        if usage is not None:
            total_usage += usage
            any_usage = True
        if cost is not None:
            total_cost += cost
            any_cost = True
        if avg is not None:
            avgs.append(avg)
    total_usage_s = f"{total_usage:.3f}" if any_usage else "-"
    total_cost_s = f"{total_cost:.2f}" if any_cost else "-"
    total_rate_s = f"{total_cost / total_usage:.4f}" if any_usage and any_cost and total_usage else "-"
    month_avg_s = f"{sum(avgs) / len(avgs):.4f}" if avgs else "-"
    lines.append(f"| **Total** | **{total_usage_s}** | **{total_cost_s}** | **{total_rate_s}** | **{month_avg_s}** |")
    if not rows:
        lines.append("")
        lines.append("_No saved days this month._")
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
    itself (Rate / Day avg spot) is derived. Pulls from the module-level
    PLAN/CAP - the account's fixed billing config, same as draw_actual()."""
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
        "Cost is the amount actually billed (as saved/entered in the app); Rate and Day avg spot "
        "are wholesale-only figures shown for comparison, not what you were charged.",
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
        self.summary_day_avg: dict[date, float] = {}
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
        self.load_fields_from_history()
        self.fetch_current()
        self.apply_actual_autofill()

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
            if rows:
                prices = [p / 1000.0 for _, p in rows]
                self.hour_stats[h] = {"avg": sum(prices) / len(prices), "n": len(rows)}

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
        rows for the summary page's region/month, and reduce them to a
        per-day average ($/kWh). Failures are recorded in
        self.summary_fetch_error rather than raised, since day usage/cost
        (the summary's main content) doesn't depend on this succeeding."""
        key = (self.region, self.summary_year, self.summary_month)
        if force:
            self.cache.pop(key, None)
        try:
            if key not in self.cache:
                self.cache[key] = qph.fetch_month(*key)
            self.summary_day_avg = day_avgs(self.cache[key])
            self.summary_fetch_error = ""
        except Exception as exc:  # noqa: BLE001 - surface any fetch failure in the UI
            self.summary_day_avg = {}
            self.summary_fetch_error = f"Spot price fetch failed: {exc}"

    def change_summary_month(self, delta: int) -> None:
        m = self.summary_month - 1 + delta
        self.summary_year += m // 12
        self.summary_month = m % 12 + 1
        self.status = ""
        self.load_summary_prices()

    def export_summary(self) -> None:
        rows = month_summary_rows(
            self.history, self.region, self.summary_year, self.summary_month, self.summary_day_avg
        )
        md = format_summary_markdown(self.region, self.summary_year, self.summary_month, rows)
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
        """Draws the day's price sparkline at row y, returns next free row."""
        if not self.day_rows:
            self.safe_addstr(y, 0, "(no price data for this day yet)")
            return y + 2

        width = max(min(w - 2, len(self.day_rows)), 1)
        buckets = chunk(self.day_rows, width)
        day_prices = [p / 1000.0 for _, p in self.day_rows]
        day_avg = sum(day_prices) / len(day_prices)
        lo, hi = min(day_prices), max(day_prices)
        spread = (hi - lo) or 1.0

        line = []
        attrs = []
        for b in buckets:
            avg = sum(p / 1000.0 for _, p in b) / len(b)
            level = min(len(SPARK) - 1, max(0, round((avg - lo) / spread * (len(SPARK) - 1))))
            spike = avg > day_avg * 1.5
            selected = any(ts in self.window_stamps for ts, _ in b)
            attr = curses.color_pair(2) if spike else curses.color_pair(1)
            if selected:
                attr |= curses.A_REVERSE
            line.append(SPARK[level])
            attrs.append(attr)

        x = 0
        for ch, attr in zip(line, attrs):
            self.safe_addstr(y, x, ch, attr)
            x += 1

        # axis labels roughly under 00:00 / 06:00 / 12:00 / 18:00 / 24:00
        for frac, label in ((0.0, "00:00"), (0.25, "06:00"), (0.5, "12:00"), (0.75, "18:00"), (1.0, "24:00")):
            lx = min(int(frac * (width - 1)), max(width - len(label), 0))
            self.safe_addstr(y + 1, lx, label, curses.A_DIM)
        return y + 3

    def draw_hour_table(self, y: int, w: int) -> int:
        """Draws a 4-column table of per-hour averages at row y, returns next free row."""
        if not self.hour_stats:
            return y

        day_vals = [p / 1000.0 for _, p in self.day_rows] if self.day_rows else []
        day_avg = sum(day_vals) / len(day_vals) if day_vals else 0.0

        cols = 4 if w >= 76 else (2 if w >= 40 else 1)
        rows = -(-24 // cols)  # ceil
        col_w = min(w // cols, 20) if cols else w

        for h in range(24):
            row, col = h % rows, h // rows
            stats = self.hour_stats.get(h)
            if stats is None:
                cell = f"{h:02d}  --"
                attr = curses.A_DIM
            else:
                incomplete = "~" if stats["n"] < 12 else " "
                cell = f"{h:02d} ${stats['avg']:.4f}{incomplete}"
                spike = day_avg and stats["avg"] > day_avg * 1.5
                attr = curses.color_pair(2) if spike else 0
            if self.hour == h:
                attr |= curses.A_REVERSE | curses.A_BOLD
            self.safe_addstr(y + row, col * col_w, cell.ljust(col_w - 1), attr)
        return y + rows + 1

    def draw_actual(self, y: int, cost: float | None) -> int:
        """If imported actual data covers any part of the current window,
        draws its exact usage/charge (no flat-load modelling - this is the
        retailer's own already-computed per-interval Wholesale Usage
        Charge) plus the network charge (computed exactly per interval from
        the real data, no Peak/Off-peak entry needed) plus fixed charges for
        a total. Controlled load is NOT added separately: T&Cs paragraph 5
        defines the billed usage as including "any controlled load, if
        applicable", and this export's Usage figure already reflects that -
        confirmed empirically (adding a flat controlled-load charge on top
        overshot two real bills by ~$0.52; without it they're within ~1.5%,
        i.e. rounding). Returns the next free row."""
        if not self.window_rows:
            return y
        stamps = [ts for ts, _ in self.window_rows]
        usage, charge, covered = self.actual_totals(stamps)
        if covered == 0:
            return y

        hours = len(self.window_rows) * (5 / 60)
        stamped_usage = [(ts, self.actual_data[ts][0]) for ts in stamps if ts in self.actual_data]
        net_charge = qph.actual_network_charge(stamped_usage, PLAN)
        core_fixed_per_day = PLAN.daily_charge + PLAN.membership_fee + (PLAN.cap_demand_kw * PLAN.cap_fee_per_kw_day)
        fixed = core_fixed_per_day * hours / 24
        total = fixed + charge + net_charge

        coverage = "full" if covered == len(stamps) else f"{covered}/{len(stamps)} intervals"
        self.safe_addstr(y, 0, f"Actual (imported, {coverage}):", curses.color_pair(4))
        y += 1
        self.safe_addstr(y, 0, f"  {usage:.4f} kWh, exact energy charge ${charge:.2f} + network ${net_charge:.2f} + fixed ${fixed:.2f}")
        y += 1
        self.safe_addstr(y, 0, f"  -> actual total: ${total:.2f}", curses.color_pair(4) | curses.A_BOLD)
        y += 1
        if cost is not None:
            diff = cost - total
            word = "over" if diff >= 0 else "under"
            self.safe_addstr(y, 0, f"  billed ${cost:.2f} is {word} the actual total by ${abs(diff):.2f}", curses.color_pair(3))
            y += 1
        return y + 1

    def draw_summary(self) -> None:
        h, w = self.stdscr.getmaxyx()
        rows = month_summary_rows(
            self.history, self.region, self.summary_year, self.summary_month, self.summary_day_avg
        )
        month_label = date(self.summary_year, self.summary_month, 1).strftime("%B %Y")
        header = f" {self.region}  {month_label}  [MONTH SUMMARY] "
        self.safe_addstr(0, 0, header.ljust(w), curses.A_REVERSE | curses.A_BOLD)

        y = 2
        if self.summary_fetch_error:
            self.safe_addstr(y, 0, self.summary_fetch_error, curses.color_pair(2))
            y += 1

        col = "{:<12}{:>14}{:>12}{:>15}{:>17}"
        rule = "-" * min(w - 1, 70)
        if not rows:
            self.safe_addstr(y, 0, "No saved days this month.", curses.A_DIM)
        else:
            self.safe_addstr(
                y, 0, col.format("Date", "Usage (kWh)", "Cost ($)", "Rate ($/kWh)", "Day avg spot"), curses.A_BOLD
            )
            y += 1
            self.safe_addstr(y, 0, rule)
            y += 1

            max_rows = max(h - 11 - y, 0)  # leave room for the totals row, footnote, status and help lines
            shown = rows[:max_rows]
            for r in shown:
                usage, cost, avg = r["usage"], r["cost"], r["avg"]
                usage_s = f"{usage:.3f}" if usage is not None else "-"
                cost_s = f"{cost:.2f}" if cost is not None else "-"
                rate_s = f"{cost / usage:.4f}" if usage and cost is not None else "-"
                avg_s = f"${avg:.4f}" if avg is not None else "-"
                self.safe_addstr(y, 0, col.format(r["date"].strftime("%a %d %b"), usage_s, cost_s, rate_s, avg_s))
                y += 1
            if len(rows) > len(shown):
                self.safe_addstr(y, 0, f"... and {len(rows) - len(shown)} more day(s) not shown (export to see all)", curses.A_DIM)
                y += 1

            total_usage = sum(r["usage"] for r in rows if r["usage"] is not None)
            total_cost = sum(r["cost"] for r in rows if r["cost"] is not None)
            any_usage = any(r["usage"] is not None for r in rows)
            any_cost = any(r["cost"] is not None for r in rows)
            avgs = [r["avg"] for r in rows if r["avg"] is not None]
            total_usage_s = f"{total_usage:.3f}" if any_usage else "-"
            total_cost_s = f"{total_cost:.2f}" if any_cost else "-"
            total_rate_s = f"{total_cost / total_usage:.4f}" if any_usage and any_cost and total_usage else "-"
            month_avg_s = f"${sum(avgs) / len(avgs):.4f}" if avgs else "-"
            self.safe_addstr(y, 0, rule)
            y += 1
            self.safe_addstr(
                y, 0, col.format("Total", total_usage_s, total_cost_s, total_rate_s, month_avg_s), curses.A_BOLD
            )
            y += 2

            p = PLAN
            fixed_daily = p.daily_charge + p.membership_fee + (p.cap_demand_kw * p.cap_fee_per_kw_day)
            footnote = [
                f"Fixed ${fixed_daily:.3f}/day (daily + membership + cap fee)",
                f"Network: peak ${p.peak_network_rate:.4f}/kWh, off-peak ${p.offpeak_network_rate:.4f}/kWh,",
                f"         shoulder ${p.shoulder_network_rate:.4f}/kWh",
                "Cost is billed, not derived from these - full breakdown in export (e)",
            ]
            for line in footnote:
                if y >= h - 2:
                    break
                self.safe_addstr(y, 0, line, curses.A_DIM)
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

        self.safe_addstr(y, 0, "Hourly averages ($/kWh; ~ = partial hour, e.g. today or a month boundary):", curses.A_DIM)
        y = self.draw_hour_table(y + 1, w)
        y += 1

        cost = parse_float(self.fields.cost)
        y = self.draw_actual(y, cost)

        if self.window_rows:
            self.safe_addstr(y, 0, "Manual entry (only needed to check a day/hour without imported data):", curses.A_DIM)
            y += 1
            values = [p / 1000.0 for _, p in self.window_rows]
            hours = len(self.window_rows) * (5 / 60)
            usage = parse_float(self.fields.usage)
            self.safe_addstr(y, 0, f"Usage [u]: {self.fields.usage or '(unset)':>10} kWh    Cost [c]: {self.fields.cost or '(unset)':>10} $")
            y += 1
            total = None
            if usage is not None:
                total, fixed, variable, capped = qph.estimate_cost(values, usage, PLAN, CAP, hours)
                self.safe_addstr(y, 0, f"  estimate: fixed ${fixed:.2f} + energy ${variable:.2f}" + (f"  ({capped} capped)" if capped else ""))
                y += 1
                self.safe_addstr(y, 0, f"  -> estimated total (incl. {PLAN.gst_rate*100:.0f}% GST): ${total:.2f}", curses.A_BOLD)
                y += 1
                if cost is not None:
                    diff = cost - total
                    word = "over" if diff >= 0 else "under"
                    self.safe_addstr(y, 0, f"  actual ${cost:.2f} is {word} the estimate by ${abs(diff):.2f}", curses.color_pair(3))
                    y += 1
            y += 1

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
