#!/usr/bin/env python3
"""Download a GloBird "Wholesale Data Export" CSV directly from the account
portal's API, instead of manually clicking Export on
https://myaccount.globirdenergy.com.au/wholesaledata and moving the file
into place by hand.

Auth: the portal's `globird-portal-user` cookie (an ASP.NET Core encrypted
auth cookie, found via browser devtools -> Network -> any request to
myaccount.globirdenergy.com.au -> Cookies) is read from
COOKIE_FILE/$GLOBIRD_SESSION_COOKIE - never hardcoded here, since this file
lives in a dotfiles repo pushed to GitHub. The cookie is a session token and
will eventually expire; when it does, every request here fails with the
portal returning its login page (HTML, not CSV) instead of data - re-copy a
fresh cookie value from the browser and overwrite COOKIE_FILE when that
happens (see fetch_csv()'s error message).

Saves into qld_price_history.ACTUAL_DATA_DIR using the same
"Wholesale Data Export <from>-<to>.csv" naming convention as a manual
export, then runs the normal sync_actual_data() merge - so this is a
drop-in replacement for the manual download step, not a separate data path.

Run:
  qld_globird_fetch.py                  # yesterday only (the usual daily case)
  qld_globird_fetch.py 09-08-2026
  qld_globird_fetch.py 05-08-2026 09-08-2026   # a range - untested whether
                                                # the API accepts more than a
                                                # single day; falls back to
                                                # one request per day if it 400s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import qld_price_history as qph

API_URL = "https://myaccount.globirdenergy.com.au/api/wholesale/generatewholesaleintervaldatacsvfile"
REQUEST_TIMEOUT = 30

# Not secret - already appear in filenames throughout this account's folder
# (e.g. "...QB031292095_...") - just the account/meter identifiers the API needs.
DEFAULT_ACCOUNT_SERVICE_ID = int(os.environ.get("GLOBIRD_ACCOUNT_SERVICE_ID", "745902"))
DEFAULT_IDENTIFIER = os.environ.get("GLOBIRD_IDENTIFIER", "QB031292095")

# Deliberately *not* under ~/dotfiles or ~/.config (a symlink into the
# dotfiles repo) or ~/braindump - both are pushed to GitHub, and this is a
# live session credential.
COOKIE_FILE = os.path.expanduser(os.environ.get("GLOBIRD_COOKIE_FILE", "~/.globird_session_cookie"))

# Shared across every place a missing/expired cookie gets reported (no
# cookie file, HTTP 401/403, or a login-page response instead of CSV) so
# qld_price_tui.py can show the exact same recovery steps in the dashboard
# instead of just "auth failed". One step per line - the caller decides how
# to render/wrap them.
REAUTH_HELP = (
    "1. Log into https://myaccount.globirdenergy.com.au in your browser\n"
    "2. Open devtools -> Network\n"
    "3. Click any request to myaccount.globirdenergy.com.au\n"
    "4. Copy the `globird-portal-user` cookie's value from its request headers\n"
    f"5. Overwrite {COOKIE_FILE} with just that value (no quotes, no trailing newline needed)"
)


def load_cookie() -> str:
    env_cookie = os.environ.get("GLOBIRD_SESSION_COOKIE")
    if env_cookie:
        return env_cookie.strip()
    try:
        with open(COOKIE_FILE) as f:
            cookie = f.read().strip()
    except OSError:
        cookie = ""
    if not cookie:
        raise SystemExit(
            f"No session cookie found (checked $GLOBIRD_SESSION_COOKIE and {COOKIE_FILE}).\n" + REAUTH_HELP
        )
    return cookie


def fetch_csv(
    from_date: date,
    to_date: date,
    cookie: str,
    account_service_id: int = DEFAULT_ACCOUNT_SERVICE_ID,
    identifier: str = DEFAULT_IDENTIFIER,
) -> bytes:
    """POSTs the same request the portal's own "Export as CSV" button makes
    and returns the raw CSV bytes. Raises RuntimeError with a clear message
    (REAUTH_HELP steps included) on either an auth-flavoured HTTP error
    (401/403 - what an invalid/expired cookie actually returns in practice)
    or a 200 response that isn't CSV (the portal returning its login page
    instead of data is also possible in principle, so Content-Type is
    checked regardless of status code)."""
    body = json.dumps(
        {
            "accountServiceId": account_service_id,
            "identifier": identifier,
            "fromDate": from_date.strftime("%Y/%m/%d"),
            "toDate": to_date.strftime("%Y/%m/%d"),
        }
    ).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": "https://myaccount.globirdenergy.com.au",
            "Referer": "https://myaccount.globirdenergy.com.au/wholesaledata",
            "User-Agent": "Mozilla/5.0",
            "Cookie": f"globird-portal-user={cookie}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            content_type = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(
                f"GloBird API returned HTTP {exc.code} ({exc.reason}) - the session cookie in "
                f"{COOKIE_FILE} has expired or is invalid.\n" + REAUTH_HELP
            ) from exc
        raise RuntimeError(f"GloBird API returned HTTP {exc.code}: {exc.reason}") from exc
    if "csv" not in content_type.lower():
        raise RuntimeError(
            f"Expected a CSV response but got Content-Type '{content_type}' - the session cookie "
            f"in {COOKIE_FILE} has likely expired.\n" + REAUTH_HELP
        )
    return data


def save_export(data: bytes, from_date: date, to_date: date) -> str:
    name = f"Wholesale Data Export {from_date:%Y%m%d}-{to_date:%Y%m%d}.csv"
    path = os.path.join(qph.ACTUAL_DATA_DIR, name)
    os.makedirs(qph.ACTUAL_DATA_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def parse_day(s: str) -> date:
    return datetime.strptime(s, "%d-%m-%Y").date()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("from_day", nargs="?", type=parse_day, help="DD-MM-YYYY, default yesterday")
    parser.add_argument("to_day", nargs="?", type=parse_day, help="DD-MM-YYYY, default same as from_day")
    args = parser.parse_args()

    from_day = args.from_day or (date.today() - timedelta(days=1))
    to_day = args.to_day or from_day

    cookie = load_cookie()
    try:
        data = fetch_csv(from_day, to_day, cookie)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    path = save_export(data, from_day, to_day)
    print(f"Saved {path}")

    merged = qph.sync_actual_data()
    print(f"Synced - {len(merged)} intervals now in the actual-data cache.")


if __name__ == "__main__":
    main()
