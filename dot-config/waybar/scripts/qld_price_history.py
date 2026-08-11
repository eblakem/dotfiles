#!/usr/bin/env python3
"""Fact-check a retailer's daily bill against AEMO's published wholesale spot price.

Pulls AEMO's official "Price and Demand" CSV for the requested month
(https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_<YYYYMM>_<REGION>.csv)
- the audited historical 5-minute dispatch price (RRP, $/MWh) for a NEM
region. This has full calendar days going back, so it can check what a
retailer bill says against what the wholesale market actually did on that
day - but it's published with a lag, so *today* is mostly empty in it
until AEMO catches up (usually the next day). When the requested day is
today, this script fills the gap by also pulling qld_price.py's live
rolling ~24h feed and merging in whatever intervals have actually
happened so far (archive data wins if the two ever overlap).

Pass --usage (without --cost) to get a rough cost *estimate* for that usage,
built from the same retail plan model as qld_price.py (fixed daily/membership/
cap fees + GST, prorated to the window, plus the actual historical spot price
applied per 5-min interval with the wholesale price cap). Pass --cost too to
see how that estimate compares to what you were actually billed for that same
window.

If your provider only gives you a daily cost (no per-hour breakdown), use
--hour with --day-usage/--day-cost instead: it estimates the whole day too
and shows what share of the day's cost that one hour is likely responsible
for.

If your bill instead breaks daily usage down by Peak/Off-peak/Shoulder (the
common case when you don't have per-hour data either), use --peak-usage /
--offpeak-usage together with --usage for the day's total (Shoulder is
derived as total - peak - offpeak, since bills often don't report it
directly), plus --controlled-usage for any flat-rate controlled load: each
period gets priced against the spot data for just its own hours, which is
a better estimate than spreading all usage evenly across the whole day.

Examples:
  qld_price_history.py                                  # yesterday, QLD1
  qld_price_history.py 06-08-2026                        # a specific day (DD-MM-YYYY)
  qld_price_history.py 06-08-2026 --usage 29.628 --cost 8.49
  qld_price_history.py 06-08-2026 --hour 7               # just the 7-8am window
  qld_price_history.py 06-08-2026 --hour 7 --usage 2.226
  qld_price_history.py 06-08-2026 --hour 7 --usage 2.226 --day-usage 29.628 --day-cost 8.49
  qld_price_history.py 06-08-2026 --peak-usage 8.1 --offpeak-usage 12.4 --usage 29.628 \\
      --controlled-usage 7.3 --cost 8.49
  qld_price_history.py 06-08-2026 --region NSW1
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import qld_price as qp

CSV_URL = "https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{ym}_{region}.csv"
REQUEST_TIMEOUT = 15

# Below this many distinct historical days of actual data, historical_hourly_usage()'s
# per-hour shape is too noisy (one unusual day could dominate a single hour's average) to
# trust over the simpler partial-actual-flat fallback - see project_interval_usage() and
# qld_price_tui.py's draw_usage_cost().
MIN_HISTORY_DAYS_FOR_PROJECTION = 2

# Where retailer "Wholesale Data Export" CSVs (actual per-interval usage +
# already-computed Wholesale Usage Charge) get dropped for qld_price_tui.py
# to pick up automatically - same folder as the account's other WholeSave
# documents. Once a CSV's data has been merged into ACTUAL_DATA_CACHE, the
# file itself is moved into ACTUAL_DATA_ARCHIVE_DIR so it isn't re-parsed
# (or left cluttering the folder) next time.
ACTUAL_DATA_DIR = os.path.expanduser(
    os.environ.get("QLD_ACTUAL_DATA_DIR", "~/braindump/personal/wholesave-globird")
)
ACTUAL_DATA_CACHE = os.path.join(ACTUAL_DATA_DIR, "qld_price_actual_data.json")
ACTUAL_DATA_ARCHIVE_DIR = os.path.join(ACTUAL_DATA_DIR, "imported")


def fetch_month(region: str, year: int, month: int) -> list[tuple[str, float]]:
    url = CSV_URL.format(ym=f"{year:04d}{month:02d}", region=region)
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        text = resp.read().decode()
    return [
        (row["SETTLEMENTDATE"], float(row["RRP"]))
        for row in csv.DictReader(io.StringIO(text))
    ]


def fetch_live_actual(region: str) -> list[tuple[str, float]]:
    """AEMO's live rolling ~24h feed (same source as qld_price.py), converted
    to the archive CSV's timestamp format ("YYYY/MM/DD HH:MM:SS")."""
    rows = qp.fetch_region_rows(region)
    return [
        (r["SETTLEMENTDATE"].replace("-", "/").replace("T", " "), float(r["RRP"]))
        for r in rows
        if r["PERIODTYPE"] == "ACTUAL"
    ]


def augment_with_live(rows: list[tuple[str, float]], region: str, day: date) -> list[tuple[str, float]]:
    """If `day` is today, merge in the live feed so intervals not yet in the
    published archive still show up. Archive rows win on any overlap; a live
    fetch failure is silently ignored (the archive-only result still stands)."""
    if day != date.today():
        return rows
    try:
        live = fetch_live_actual(region)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
        return rows
    have = {t for t, _ in rows}
    return rows + [(t, p) for t, p in live if t not in have]


def parse_globird_export(path: str) -> dict[str, tuple[float, float]]:
    """Parse a GloBird "Wholesale Data Export" CSV (columns: Date, Time,
    Usage (kWh), Wholesale Price (Cents/kWh, Gst incl), DLF, MLF, Wholesale
    Usage Charge (Cents, Gst incl)) into {timestamp: (usage_kwh, charge_$)}.

    Timestamps use the same "YYYY/MM/DD HH:MM:SS" key format as the AEMO
    archive data - Time is end-of-interval, the same convention as
    SETTLEMENTDATE - so this can be looked up the same way (hour_window(),
    day_prices(), etc). The Wholesale Usage Charge is GST-inclusive and
    already has the account's price cap/DLF/MLF applied by the retailer, so
    unlike wholesale_usage_charge() it needs no further modelling - just add
    fixed charges on top for a total. Files that don't match this format
    (wrong header) yield an empty dict rather than raising."""
    out: dict[str, tuple[float, float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Usage (kWh)" not in reader.fieldnames:
            return out
        for row in reader:
            try:
                dt = datetime.strptime(f"{row['Date']} {row['Time']}", "%d-%b-%Y %H:%M")
                usage = float(row["Usage (kWh)"])
                charge_cents = float(row["Wholesale Usage Charge (Cents, Gst incl)"])
            except (ValueError, KeyError):
                continue
            out[dt.strftime("%Y/%m/%d %H:%M:%S")] = (usage, charge_cents / 100.0)
    return out


def load_actual_data_cache() -> dict[str, tuple[float, float]]:
    """The merged {timestamp: (usage_kwh, charge_$)} store built up from
    every GloBird export CSV imported so far - see sync_actual_data()."""
    try:
        with open(ACTUAL_DATA_CACHE) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {ts: (v[0], v[1]) for ts, v in raw.items()}


def save_actual_data_cache(data: dict[str, tuple[float, float]]) -> None:
    os.makedirs(os.path.dirname(ACTUAL_DATA_CACHE), exist_ok=True)
    tmp = ACTUAL_DATA_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({ts: list(v) for ts, v in sorted(data.items())}, f)
    os.replace(tmp, ACTUAL_DATA_CACHE)


def sync_actual_data(dir_path: str = ACTUAL_DATA_DIR) -> dict[str, tuple[float, float]]:
    """Load the persistent actual-data cache, merge in any new GloBird
    export CSVs sitting directly in `dir_path` (not its archive
    subfolder), archive each one into `dir_path`/imported/ once merged, and
    persist the updated cache. Returns the full merged dataset. Safe to
    call repeatedly (e.g. on every TUI refresh) - nothing new to import is
    a no-op."""
    data = load_actual_data_cache()
    try:
        names = [n for n in os.listdir(dir_path) if n.lower().endswith(".csv")]
    except OSError:
        return data

    changed = False
    for name in sorted(names):
        path = os.path.join(dir_path, name)
        try:
            parsed = parse_globird_export(path)
        except OSError:
            continue
        if not parsed:
            continue
        data.update(parsed)
        changed = True

        os.makedirs(ACTUAL_DATA_ARCHIVE_DIR, exist_ok=True)
        dest = os.path.join(ACTUAL_DATA_ARCHIVE_DIR, name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            n = 2
            while os.path.exists(dest):
                dest = os.path.join(ACTUAL_DATA_ARCHIVE_DIR, f"{stem} ({n}){ext}")
                n += 1
        os.replace(path, dest)

    if changed:
        save_actual_data_cache(data)
    return data


def parse_day(text: str) -> date:
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date {text!r}, expected DD-MM-YYYY")


def day_prices(rows: list[tuple[str, float]], day: date) -> list[tuple[str, float]]:
    prefix = day.strftime("%Y/%m/%d")
    return [(t, p) for t, p in rows if t.startswith(prefix)]


def hour_window(day: date, hour: int) -> tuple[datetime, datetime, list[str]]:
    # AEMO SETTLEMENTDATE labels the *end* of each 5-min interval, so the
    # clock hour [hour:00, hour+1:00) is covered by the 12 timestamps
    # hour:05 through (hour+1):00.
    start = datetime(day.year, day.month, day.day, hour, 0)
    end = start + timedelta(hours=1)
    stamps = []
    t = start + timedelta(minutes=5)
    while t <= end:
        stamps.append(t.strftime("%Y/%m/%d %H:%M:%S"))
        t += timedelta(minutes=5)
    return start, end, stamps


# TOU windows, tou_period_for_hour(), network_rate_for_period() and
# network_charge() now live in qld_price.py (as qp.TOU_PEAK_HOURS etc) so
# the live waybar module and this script share one definition.


def hours_prices(day: date, hours: range | list[int], lookup: dict[str, float]) -> list[tuple[str, float]]:
    """All 5-min interval rows for the given clock hours of `day`, correctly
    handling the day-boundary quirk in hour_window() (e.g. hour 23 needs an
    entry dated the next day)."""
    rows = []
    for h in hours:
        _, _, stamps = hour_window(day, h)
        rows.extend((ts, lookup[ts]) for ts in stamps if ts in lookup)
    return rows


def interval_hour(ts: str) -> int:
    """Which clock hour (0-23) a 5-min interval timestamp belongs to, using
    the same end-of-interval convention as hour_window(): an interval
    timestamped HH:00 belongs to hour HH-1, not HH."""
    hh, mm = int(ts[11:13]), int(ts[14:16])
    return (hh - 1) % 24 if mm == 0 else hh


def actual_network_charge_by_period(stamped_usage: list[tuple[str, float]], plan: qp.Plan) -> dict[str, float]:
    """Exact network charge for real per-interval (timestamp, usage_kwh)
    pairs (as found in the imported actual-data cache), split by TOU meter
    - peak/off-peak/shoulder (see qp.tou_period_for_hour()) - each interval
    classified from its own clock hour via interval_hour()."""
    totals = {"peak": 0.0, "offpeak": 0.0, "shoulder": 0.0}
    for ts, usage in stamped_usage:
        period = qp.tou_period_for_hour(interval_hour(ts))
        totals[period] += usage * qp.network_rate_for_period(period, plan)
    return totals


def actual_network_charge(stamped_usage: list[tuple[str, float]], plan: qp.Plan) -> float:
    """Total across all three TOU meters - see actual_network_charge_by_period()."""
    return sum(actual_network_charge_by_period(stamped_usage, plan).values())


def estimated_network_charge_by_period(stamps: list[str], usage_kwh: float, plan: qp.Plan) -> dict[str, float]:
    """Estimated network charge for `usage_kwh` spread evenly across
    `stamps` - the same flat-load assumption estimate_cost() already uses
    for the wholesale energy charge, just extended to cover network too -
    split by TOU meter, same as actual_network_charge_by_period(). Less
    trustworthy than the actual variant whenever usage isn't actually flat
    across the window (e.g. a controlled-load spike concentrated in one TOU
    period)."""
    n = len(stamps)
    totals = {"peak": 0.0, "offpeak": 0.0, "shoulder": 0.0}
    if n == 0:
        return totals
    flat_usage = usage_kwh / n
    for ts in stamps:
        period = qp.tou_period_for_hour(interval_hour(ts))
        totals[period] += flat_usage * qp.network_rate_for_period(period, plan)
    return totals


def estimated_network_charge(stamps: list[str], usage_kwh: float, plan: qp.Plan) -> float:
    """Total across all three TOU meters - see estimated_network_charge_by_period()."""
    return sum(estimated_network_charge_by_period(stamps, usage_kwh, plan).values())


def wholesale_usage_charge(
    prices_kwh: list[float], usage_kwh: float, plan: qp.Plan, cap: float, hours: float
) -> tuple[float, int]:
    """The Wholesale Usage Charge (T&Cs paragraph 5) for `usage_kwh` spread
    evenly across the given per-interval wholesale prices, applying the
    plan's price cap per-interval (same rule as Plan.estimate_hourly_cost:
    only protects usage while demand stays within cap_demand_kw) and the
    account's loss factors (plan.dlf x plan.mlf). This energy charge is
    GST-exclusive per T&Cs paragraph 8, so GST is added here (unlike the
    fixed charges, which are already GST-inclusive - see estimate_cost).
    Returns (charge_incl_gst, intervals_capped)."""
    n = len(prices_kwh)
    if n == 0:
        return 0.0, 0
    energy_per_interval = usage_kwh / n
    interval_hours = hours / n
    load_kw = energy_per_interval / interval_hours if interval_hours else 0.0
    within_cap_demand = load_kw <= plan.cap_demand_kw

    raw = 0.0
    capped = 0
    for price in prices_kwh:
        if price > 0 and within_cap_demand:
            effective = min(price, cap)
            if effective < price:
                capped += 1
        else:
            effective = price
        raw += effective * energy_per_interval
    raw *= plan.dlf * plan.mlf
    return raw * (1 + plan.gst_rate), capped


def estimate_cost(
    prices_kwh: list[float], usage_kwh: float, plan: qp.Plan, cap: float, hours: float
) -> tuple[float, float, float, int]:
    """Estimate total cost (fixed charges + Wholesale Usage Charge) for
    `usage_kwh` spread evenly across the given per-interval wholesale prices
    over `hours`. Returns (total, fixed, variable, intervals_capped)."""
    variable, capped = wholesale_usage_charge(prices_kwh, usage_kwh, plan, cap, hours)
    fixed = plan.fixed_cost_per_hour * hours  # already GST-inclusive
    return fixed + variable, fixed, variable, capped


def historical_hourly_usage(
    actual_data: dict[str, tuple[float, float]], exclude_day: date | None = None
) -> tuple[dict[int, float], int]:
    """Average per-5-minute-interval usage (kWh) for each hour-of-day
    (0-23), built from every day of real imported readings in `actual_data`
    (see sync_actual_data()) - the day-to-day "usage shape" project_interval_
    usage() uses to fill in hours that don't have actual data yet, instead
    of assuming a flat/even load across the whole day. `exclude_day`
    (normally the in-progress day being projected) is left out of the
    average so its own already-known hours don't bias the shape used to
    project its remaining ones. Returns (profile, n_days) - n_days is how
    many distinct calendar days contributed, so callers can decide whether
    there's enough history to trust the shape (see
    MIN_HISTORY_DAYS_FOR_PROJECTION)."""
    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    days: set[date] = set()
    for ts, (usage, _charge) in actual_data.items():
        d = datetime.strptime(ts[:10], "%Y/%m/%d").date()
        if exclude_day is not None and d == exclude_day:
            continue
        days.add(d)
        buckets[interval_hour(ts)].append(usage)
    profile = {h: sum(v) / len(v) for h, v in buckets.items() if v}
    return profile, len(days)


def project_interval_usage(
    stamps: list[str],
    actual_data: dict[str, tuple[float, float]],
    hourly_profile: dict[int, float],
) -> tuple[list[float], int]:
    """Per-interval usage (kWh) for each of `stamps`, in order: the real
    reading from `actual_data` where available, else that interval's
    hour-of-day historical average from `hourly_profile` (see
    historical_hourly_usage()) - falling back to the profile's overall
    average for an hour that has no historical reading of its own (e.g. a
    rarely-observed hour). Returns (usages, n_projected) - n_projected is
    how many of the returned values are projections rather than real
    readings."""
    overall = sum(hourly_profile.values()) / len(hourly_profile) if hourly_profile else 0.0
    usages = []
    n_projected = 0
    for ts in stamps:
        got = actual_data.get(ts)
        if got is not None:
            usages.append(got[0])
        else:
            usages.append(hourly_profile.get(interval_hour(ts), overall))
            n_projected += 1
    return usages, n_projected


def wholesale_usage_charge_weighted(
    prices_kwh: list[float], usages_kwh: list[float], plan: qp.Plan, cap: float, hours: float
) -> tuple[float, int]:
    """Like wholesale_usage_charge(), but priced from each interval's own
    usage (`usages_kwh`, same order/length as `prices_kwh` - see
    project_interval_usage()) instead of one total spread evenly, so a
    usage shape that isn't flat (e.g. an evening spike) prices against that
    interval's own spot price and demand-cap status, not an averaged one.
    Returns (charge_incl_gst, intervals_capped)."""
    n = len(prices_kwh)
    if n == 0:
        return 0.0, 0
    interval_hours = hours / n
    raw = 0.0
    capped = 0
    for price, usage in zip(prices_kwh, usages_kwh):
        load_kw = usage / interval_hours if interval_hours else 0.0
        within_cap_demand = load_kw <= plan.cap_demand_kw
        if price > 0 and within_cap_demand:
            effective = min(price, cap)
            if effective < price:
                capped += 1
        else:
            effective = price
        raw += effective * usage
    raw *= plan.dlf * plan.mlf
    return raw * (1 + plan.gst_rate), capped


def estimate_cost_weighted(
    prices_kwh: list[float], usages_kwh: list[float], plan: qp.Plan, cap: float, hours: float
) -> tuple[float, float, float, int]:
    """Like estimate_cost(), but priced from per-interval usage instead of
    a flat total - see wholesale_usage_charge_weighted(). Returns (total,
    fixed, variable, intervals_capped)."""
    variable, capped = wholesale_usage_charge_weighted(prices_kwh, usages_kwh, plan, cap, hours)
    fixed = plan.fixed_cost_per_hour * hours  # already GST-inclusive
    return fixed + variable, fixed, variable, capped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("day", nargs="?", help="Date to check, DD-MM-YYYY (default: yesterday)")
    parser.add_argument("--region", default="QLD1", help="NEM region ID (default: %(default)s)")
    parser.add_argument("--hour", type=int, help="Restrict to one clock hour (0-23), e.g. --hour 7 checks 07:00-08:00")
    parser.add_argument("--usage", type=float, help="Actual/retailer-reported usage for the day/window, in kWh")
    parser.add_argument("--cost", type=float, help="Retailer-reported cost for the day/window, in $, to compare against")
    parser.add_argument(
        "--day-usage", type=float,
        help="Total usage for the whole day, in kWh (use with --hour, when your provider only "
             "reports a daily cost rather than one per hour -- lets this hour's estimate be shown "
             "as a share of the day's estimate/bill instead)",
    )
    parser.add_argument(
        "--day-cost", type=float,
        help="Total actual cost for the whole day, in $ (use with --hour and --day-usage)",
    )
    parser.add_argument(
        "--peak-usage", type=float,
        help=f"Day's Peak-period usage in kWh ({qp.TOU_PEAK_HOURS.start:02d}:00-{qp.TOU_PEAK_HOURS.stop:02d}:00). "
             "Day-level only (no --hour); use with --offpeak-usage and --usage (the day's total, "
             "used to derive Shoulder = total - peak - offpeak).",
    )
    parser.add_argument(
        "--offpeak-usage", type=float,
        help=f"Day's Off-peak-period usage in kWh ({qp.TOU_OFFPEAK_HOURS.start:02d}:00-{qp.TOU_OFFPEAK_HOURS.stop:02d}:00).",
    )
    parser.add_argument(
        "--controlled-usage", type=float,
        help="Day's actual Controlled Load usage in kWh - informational only for WholeSave "
             "(T&Cs bill it as part of general usage, not separately; see --controlled-load-rate "
             "if a different plan does bill it flat). Day-level only.",
    )
    parser.add_argument("--top", type=int, default=3, help="Show this many highest-price intervals (default: %(default)s)")
    parser.add_argument(
        "--cap", type=float, default=float(os.environ.get("QLD_PRICE_CAP", qp.DEFAULT_CAP)),
        help="Wholesale price cap in $/kWh, used for --usage estimates (default: %(default)s, env QLD_PRICE_CAP)",
    )
    parser.add_argument(
        "--cap-demand-kw", type=float,
        default=float(os.environ.get("QLD_CAP_DEMAND_KW", qp.DEFAULT_CAP_DEMAND_KW)),
        help="Purchased wholesale cap demand in kW (default: %(default)s, env QLD_CAP_DEMAND_KW)",
    )
    parser.add_argument(
        "--cap-fee-per-kw-day", type=float,
        default=float(os.environ.get("QLD_CAP_FEE_PER_KW_DAY", qp.DEFAULT_CAP_FEE_PER_KW_DAY)),
        help="Wholesale cap fee in $/kW/day (default: %(default)s)",
    )
    parser.add_argument(
        "--daily-charge", type=float,
        default=float(os.environ.get("QLD_DAILY_CHARGE", qp.DEFAULT_DAILY_CHARGE)),
        help="Daily charge in $/day (default: %(default)s)",
    )
    parser.add_argument(
        "--membership-fee", type=float,
        default=float(os.environ.get("QLD_MEMBERSHIP_FEE", qp.DEFAULT_MEMBERSHIP_FEE)),
        help="Membership fee in $/day (default: %(default)s)",
    )
    parser.add_argument(
        "--controlled-load-kwh-per-day", type=float,
        default=float(
            os.environ.get("QLD_CONTROLLED_LOAD_KWH_PER_DAY", qp.DEFAULT_CONTROLLED_LOAD_KWH_PER_DAY)
        ),
        help="Controlled load usage in kWh/day, billed flat (default: %(default).2f)",
    )
    parser.add_argument(
        "--controlled-load-rate", type=float,
        default=float(os.environ.get("QLD_CONTROLLED_LOAD_RATE", qp.DEFAULT_CONTROLLED_LOAD_RATE)),
        help="Controlled load rate in $/kWh (default: %(default)s)",
    )
    parser.add_argument(
        "--gst-rate", type=float, default=float(os.environ.get("QLD_GST_RATE", qp.DEFAULT_GST_RATE)),
        help="GST rate applied to the wholesale usage (energy) charge only (default: %(default)s)",
    )
    parser.add_argument(
        "--dlf", type=float, default=float(os.environ.get("QLD_DLF", qp.DEFAULT_DLF)),
        help="Distribution Loss Factor applied to the wholesale usage charge (default: %(default)s)",
    )
    parser.add_argument(
        "--mlf", type=float, default=float(os.environ.get("QLD_MLF", qp.DEFAULT_MLF)),
        help="Marginal Loss Factor applied to the wholesale usage charge (default: %(default)s)",
    )
    parser.add_argument(
        "--peak-network-rate", type=float,
        default=float(os.environ.get("QLD_PEAK_NETWORK_RATE", qp.DEFAULT_PEAK_NETWORK_RATE)),
        help="Network/distribution charge for Peak usage, $/kWh, on top of the wholesale charge "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--offpeak-network-rate", type=float,
        default=float(os.environ.get("QLD_OFFPEAK_NETWORK_RATE", qp.DEFAULT_OFFPEAK_NETWORK_RATE)),
        help="Network/distribution charge for Off-peak usage, $/kWh (default: %(default)s)",
    )
    parser.add_argument(
        "--shoulder-network-rate", type=float,
        default=float(os.environ.get("QLD_SHOULDER_NETWORK_RATE", qp.DEFAULT_SHOULDER_NETWORK_RATE)),
        help="Network/distribution charge for Shoulder usage, $/kWh (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.hour is not None and not (0 <= args.hour <= 23):
        parser.error("--hour must be between 0 and 23")
    if args.hour is None and (args.day_usage is not None or args.day_cost is not None):
        parser.error("--day-usage/--day-cost only make sense together with --hour")
    if args.day_cost is not None and args.day_usage is None:
        parser.error("--day-cost needs --day-usage too, to estimate the day's total cost")

    tou_mode = args.peak_usage is not None or args.offpeak_usage is not None
    if tou_mode and (args.peak_usage is None or args.offpeak_usage is None):
        parser.error("--peak-usage and --offpeak-usage must be given together")
    if tou_mode and args.usage is None:
        parser.error("--peak-usage/--offpeak-usage need --usage too (the day's total, to derive Shoulder)")
    if (tou_mode or args.controlled_usage is not None) and args.hour is not None:
        parser.error("--peak/--offpeak/--controlled-usage are day-level only (no --hour)")
    if args.controlled_usage is not None and not tou_mode and args.usage is None:
        parser.error("--controlled-usage needs --usage, or --peak-usage/--offpeak-usage, to go with it")

    try:
        day = parse_day(args.day) if args.day else date.today() - timedelta(days=1)
    except ValueError as exc:
        parser.error(str(exc))

    months_needed = {(day.year, day.month)}
    window_end = None
    if args.hour is not None:
        _, window_end, _ = hour_window(day, args.hour)
        months_needed.add((window_end.year, window_end.month))
    if tou_mode:
        # Shoulder wraps past midnight (hour 23), which needs an interval
        # dated the next day - see hour_window()'s day-boundary note.
        next_day = day + timedelta(days=1)
        months_needed.add((next_day.year, next_day.month))

    all_rows: list[tuple[str, float]] = []
    try:
        for year, month in months_needed:
            all_rows.extend(fetch_month(args.region, year, month))
    except (urllib.error.URLError, OSError) as exc:
        print(f"Failed to fetch AEMO price data: {exc}", file=sys.stderr)
        sys.exit(1)

    all_rows = augment_with_live(all_rows, args.region, day)
    day_rows = day_prices(all_rows, day)
    label = day.strftime("%d-%m-%Y")

    if args.hour is not None:
        _, window_end, stamps = hour_window(day, args.hour)
        lookup = dict(all_rows)
        prices = [(ts, lookup[ts]) for ts in stamps if ts in lookup]
        missing = len(stamps) - len(prices)
        label += f" {args.hour:02d}:00-{window_end.strftime('%H:%M')}"
        if missing:
            label += f" ({missing} interval{'s' if missing != 1 else ''} missing)"
    else:
        prices = day_rows

    if not prices:
        print(
            f"No data for {label} in region {args.region} (too far in the future, "
            "or AEMO hasn't published this month's file yet).",
            file=sys.stderr,
        )
        sys.exit(1)

    values = [p / 1000.0 for _, p in prices]  # $/kWh
    avg = sum(values) / len(values)
    lo, hi = min(values), max(values)

    print(f"{args.region} wholesale spot price -- {label} ({len(prices)} x 5-min intervals)")
    print(f"  average: ${avg:.4f}/kWh (${avg * 1000:.2f}/MWh)")
    print(f"  range:   ${lo:.4f} to ${hi:.4f}/kWh")

    top = sorted(prices, key=lambda x: -x[1])[: args.top]
    print(f"  top {len(top)} intervals:")
    for t, p in top:
        print(f"    {t.split(' ')[1]}  ${p / 1000:.4f}/kWh (${p:.2f}/MWh)")

    if args.hour is not None and day_rows:
        day_vals = [p / 1000.0 for _, p in day_rows]
        day_avg = sum(day_vals) / len(day_vals)
        print(f"  (full day average for comparison: ${day_avg:.4f}/kWh)")

    if tou_mode:
        plan = qp.Plan(
            cap_demand_kw=args.cap_demand_kw,
            cap_fee_per_kw_day=args.cap_fee_per_kw_day,
            daily_charge=args.daily_charge,
            membership_fee=args.membership_fee,
            load_kw=0.0,
            controlled_load_kwh_per_day=args.controlled_load_kwh_per_day,
            controlled_load_rate=args.controlled_load_rate,
            gst_rate=args.gst_rate,
            dlf=args.dlf,
            mlf=args.mlf,
            peak_network_rate=args.peak_network_rate,
            offpeak_network_rate=args.offpeak_network_rate,
            shoulder_network_rate=args.shoulder_network_rate,
        )
        shoulder_usage = args.usage - args.peak_usage - args.offpeak_usage
        shoulder_note = f" (derived: {args.usage:.3f} - {args.peak_usage:.3f} - {args.offpeak_usage:.3f})"
        if shoulder_usage < 0:
            print(
                f"Warning: derived Shoulder usage is negative ({shoulder_usage:.3f} kWh) - "
                "check your --usage/--peak-usage/--offpeak-usage figures.",
                file=sys.stderr,
            )

        lookup = dict(all_rows)
        periods = [
            ("Peak    (16:00-21:00)", qp.TOU_PEAK_HOURS, args.peak_usage, ""),
            ("Off-peak (09:00-16:00)", qp.TOU_OFFPEAK_HOURS, args.offpeak_usage, ""),
            ("Shoulder (21:00-09:00)", qp.TOU_SHOULDER_HOURS, shoulder_usage, shoulder_note),
        ]

        print()
        print("Estimated cost for the day, by usage period:")
        total_variable = 0.0
        total_usage = 0.0
        for period_label, period_hours_range, usage, note in periods:
            period_rows = hours_prices(day, period_hours_range, lookup)
            if not period_rows:
                print(f"  {period_label}: no price data for this period, skipped")
                continue
            period_values = [p / 1000.0 for _, p in period_rows]
            period_hours = len(period_rows) * (5 / 60)
            variable, capped = wholesale_usage_charge(period_values, usage, plan, args.cap, period_hours)
            period_avg = sum(period_values) / len(period_values)
            capped_note = f"  ({capped}/{len(period_rows)} intervals capped)" if capped else ""
            print(f"  {period_label}: {usage:.3f} kWh @ avg ${period_avg:.4f}/kWh -> ${variable:.2f}{capped_note}{note}")
            total_variable += variable
            total_usage += usage

        if args.controlled_usage is not None:
            # Informational only, not added to the total: T&Cs paragraph 5
            # bills controlled load as part of general usage under
            # WholeSave (confirmed against real per-interval data), not as
            # a separate flat charge, so it's already inside Peak/Off-peak/
            # Shoulder above if it was part of what you counted into --usage.
            print(
                f"  Controlled load: {args.controlled_usage:.3f} kWh (informational - already billed via "
                "Peak/Off-peak/Shoulder above, not added)"
            )

        net_charge = qp.network_charge(args.peak_usage, args.offpeak_usage, shoulder_usage, plan)
        print(
            f"  Network charge (peak ${plan.peak_network_rate:.4f}/kWh + off-peak "
            f"${plan.offpeak_network_rate:.4f}/kWh, on top of the wholesale charge): ${net_charge:.2f}"
        )

        fixed = plan.fixed_cost_per_hour * 24
        total = fixed + total_variable + net_charge
        print(f"  Fixed charges (daily + membership + cap fee): ${fixed:.2f}")
        print(f"  -> Estimated total: ${total:.2f}   ({total_usage:.3f} kWh total)")

        if args.cost is not None:
            diff = args.cost - total
            print(
                f"\nActual billed: ${args.cost:.2f}  (estimate ${total:.2f}, "
                f"{'over' if diff >= 0 else 'under'} estimate by ${abs(diff):.2f})"
            )
    elif args.usage is not None:
        plan = qp.Plan(
            cap_demand_kw=args.cap_demand_kw,
            cap_fee_per_kw_day=args.cap_fee_per_kw_day,
            daily_charge=args.daily_charge,
            membership_fee=args.membership_fee,
            load_kw=0.0,  # unused here; per-interval load is derived from --usage instead
            controlled_load_kwh_per_day=args.controlled_load_kwh_per_day,
            controlled_load_rate=args.controlled_load_rate,
            gst_rate=args.gst_rate,
            dlf=args.dlf,
            mlf=args.mlf,
            peak_network_rate=args.peak_network_rate,
            offpeak_network_rate=args.offpeak_network_rate,
            shoulder_network_rate=args.shoulder_network_rate,
        )
        hours = len(prices) * (5 / 60)
        total, fixed, variable, capped = estimate_cost(values, args.usage, plan, args.cap, hours)

        net_charge = None
        if args.hour is not None:
            period = qp.tou_period_for_hour(args.hour)
            net_charge = args.usage * qp.network_rate_for_period(period, plan)
            total += net_charge

        print()
        print(f"Estimated cost for {args.usage:.4f} kWh over this window (flat/even-load assumption):")
        print(f"  fixed charges ({hours:.2f}h of daily/membership/cap fees): ${fixed:.2f}")
        print(f"  energy charges @ actual spot price per interval: ${variable:.2f}")
        if capped:
            print(f"  ({capped}/{len(prices)} intervals were above the ${args.cap:.2f}/kWh cap and capped)")
        if net_charge is not None:
            print(f"  network charge ({period} rate): ${net_charge:.2f}")
        else:
            print(
                "  (network/distribution charge not included - whole-day usage isn't split by "
                "Peak/Off-peak, so it can't be applied; see --peak-usage/--offpeak-usage)"
            )
        print(f"  incl. {args.gst_rate * 100:.0f}% GST -> estimated total: ${total:.2f}")

        if args.day_usage is not None:
            day_hours = len(day_rows) * (5 / 60) if day_rows else 24.0
            day_values = [p / 1000.0 for _, p in day_rows]
            day_total, _, _, _ = estimate_cost(day_values, args.day_usage, plan, args.cap, day_hours)
            share = total / day_total if day_total else 0.0
            print()
            print(
                f"Whole day: {args.day_usage:.4f} kWh -> estimated ${day_total:.2f} "
                f"({day_hours:.1f}h modelled) -- this hour is ~{share * 100:.1f}% of that estimate."
            )
            if args.day_cost is not None:
                allocated = share * args.day_cost
                print(
                    f"Scaled to your actual daily bill of ${args.day_cost:.2f}, this hour's "
                    f"share works out to roughly ${allocated:.2f}."
                )

        if args.cost is not None:
            diff = args.cost - total
            print(
                f"\nActual billed (for this window): ${args.cost:.2f}  (estimate ${total:.2f}, "
                f"{'over' if diff >= 0 else 'under'} estimate by ${abs(diff):.2f})"
            )
            implied = args.cost / args.usage
            wdiff = implied - avg
            gap_note = (
                "some gap above the wholesale average is normal (retailer margin); the estimate "
                "above tries to account for the known fixed-fee and network-charge part of that gap."
                if net_charge is not None
                else "some gap above the wholesale average is normal (network/distribution charges "
                "not modelled here, plus retailer margin); pass --peak-usage/--offpeak-usage to "
                "have network charges included too."
            )
            print(
                f"Implied rate: ${implied:.4f}/kWh vs wholesale average ${avg:.4f}/kWh "
                f"({'+' if wdiff >= 0 else ''}{wdiff:.4f}/kWh) -- {gap_note}"
            )
        elif args.day_usage is None:
            print(
                "\nPass --cost too (if your provider gives a per-window cost) or --day-usage / "
                "--day-cost (if it only gives a daily total) to compare against a bill."
            )
    elif args.cost is not None:
        print("\nPass --usage too to get a cost estimate / comparison.", file=sys.stderr)


if __name__ == "__main__":
    main()
