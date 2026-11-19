#!/usr/bin/env python3
"""
refresh_earnings_cloud.py — rebuild earnings.json from Sensibull and push it to
the F&O Monitor Gist. Designed to run unattended on GitHub Actions.

    python3 scripts/refresh_earnings_cloud.py              # fetch, guard, push
    python3 scripts/refresh_earnings_cloud.py --dry-run    # fetch + guard, push nothing
    python3 scripts/refresh_earnings_cloud.py --force      # override the guards

Environment:
    GIST_TOKEN            required to push. Fine-grained token, Gists read+write.
    DISCORD_WEBHOOK_URL   optional. A failure posts one message here.

Stdlib only. Sensibull needs no account — an anonymous session token is issued by
the identify endpoint. The endpoint 403s without browser-ish headers; those are
set below and are the whole trick.

WHY THE GUARD COMPARES FUTURE-DATED NAMES, NOT TOTALS
-----------------------------------------------------
The Mac's push_gist.py refuses when the new file has fewer TOTAL entries than the
live one. That misfires here. Each run asks Sensibull for a window starting
today, so names that have already reported drop off the back: on 2026-10-30 a
correct refresh returned 83 entries against a live file of 115, and the total
check would have refused it — while zero future-dated names were lost and the new
file actually had MORE of them (71 vs 64).

Losing a name that has already reported is housekeeping. Losing a name that is
still due to report is the dangerous case, because that is a position whose
earnings warning silently disappears. So that is what this guard checks.

AN EMPTY MAP IS NORMAL BETWEEN RESULTS SEASONS
----------------------------------------------
After the quarterly deadline (14 Nov, 14 Feb, 30 May, 14 Aug) Sensibull can have
no results dates at all for weeks. On 2026-11-17 and -18 it returned 0 results
and 4 dividends, and the old guard refused because the live file still held 8
names. All 8 had reported on 13-14 Nov, so nothing still ahead would have been
lost. The guard sent a false alarm every day.

An empty map is now pushed when (a) no live name is still due to report and
(b) Sensibull still accepts `stock_results` as an event type. Check (b) is one
extra call made only on empty days. It sends `event_types: ["stock_results"]`,
and Sensibull answers an unknown type with HTTP 422 and the list of types it
does accept. So if they rename or drop it, the run fails loudly. Without (b), a
renamed type would look exactly like a quiet calendar, and every later push
would be an empty map.
"""

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request

GIST_ID = "c5eb3c6a51b620bec116849afd83cd70"
GIST_FILE = "earnings.json"
GIST_RAW = ("https://gist.githubusercontent.com/aggarwalsarthak13-commits/"
            + GIST_ID + "/raw/" + GIST_FILE)
GIST_API = "https://api.github.com/gists/" + GIST_ID

IDENTIFY = ("https://oxide.sensibull.com/v1/pluto/auth/web/session"
            "/a/platform/identify")
EVENTS = "https://oxide.sensibull.com/v1/compute/market_stock_events"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Origin": "https://web.sensibull.com",
    "Referer": "https://web.sensibull.com/",
    "Content-Type": "application/json",
}
# Sensibull clamps the window to its own available_end_date, so asking wide is
# free — it returns what it has and reports the real bounds back.
LOOKAHEAD_DAYS = 90
RESULTS_TYPE = "stock_results"


# ---------------------------------------------------------------- Sensibull

def _session():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    req = urllib.request.Request(IDENTIFY, headers={"User-Agent": UA})
    with opener.open(req, timeout=30) as r:
        r.read()

    token = next((c.value for c in jar if c.name == "access_token"), None)
    if not token:
        raise RuntimeError("no access_token cookie — identify endpoint changed?")
    return opener, token


def _events(opener, token, days_ahead, event_types=None):
    today = dt.date.today()
    body = {
        "start_date": today.isoformat(),
        "end_date": (today + dt.timedelta(days=days_ahead)).isoformat(),
    }
    if event_types:
        body["event_types"] = event_types

    headers = dict(HEADERS)
    headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(EVENTS, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with opener.open(req, timeout=30) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        raise RuntimeError("events call HTTP %s: %s" % (e.code, detail))

    if not payload.get("success"):
        raise RuntimeError("events call failed: %s" % payload.get("errors"))
    return payload["payload"]


def fetch(days_ahead):
    opener, token = _session()
    return _events(opener, token, days_ahead)


def confirm_results_type(days_ahead):
    """Raise unless Sensibull still accepts RESULTS_TYPE as an event type.

    Called only when the results map is empty, to tell a quiet calendar from
    a renamed or removed type. Sensibull rejects an unknown type with HTTP 422
    and lists the types it does accept.
    """
    opener, token = _session()
    try:
        _events(opener, token, days_ahead, event_types=[RESULTS_TYPE])
    except RuntimeError as e:
        raise RuntimeError("0 results returned AND Sensibull no longer accepts "
                           "'%s' as an event type, so the API has changed. "
                           "Not blanking the Earnings column. %s"
                           % (RESULTS_TYPE, e))


def build(p):
    rows = [e for e in p.get("data", []) if e.get("event_type") == "stock_results"]
    rows.sort(key=lambda e: (e["date"], e["trading_symbol"]))
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()
                          .replace("+00:00", "Z"),
        "window_from": p.get("from_date"),
        "window_to": p.get("to_date"),
        "earnings": {e["trading_symbol"]: e["date"] for e in rows},
    }


# ---------------------------------------------------------------- the Gist

def read_live():
    url = GIST_RAW + "?t=" + str(int(dt.datetime.now().timestamp()))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        print("  (could not read the live Gist copy: %s)" % e)
        return None


def future(m, today=None):
    """Symbols in an earnings map whose date is today or later."""
    today = today or dt.date.today()
    out = set()
    for s, d in m.items():
        try:
            if dt.date.fromisoformat(d) >= today:
                out.add(s)
        except (TypeError, ValueError):
            pass
    return out


def guard(new, live, force):
    """Return (ok, reason). Refuses a push that would lose information.

    An empty new map is allowed only when no live name is still due to
    report. main() has already checked that Sensibull still accepts the
    results event type.
    """
    nn = new["earnings"]
    live_map = (live or {}).get("earnings") or {}

    if not nn:
        ahead = sorted(future(live_map))
        if ahead:
            return False, ("Sensibull returned an EMPTY map while %d live name(s) "
                           "are still due to report (%s). That blanks their "
                           "earnings warnings, so refusing."
                           % (len(ahead), ", ".join(ahead[:12])))
        if live_map:
            print("  empty map: no results dates in Sensibull's window. All %d live "
                  "entries have already reported, so nothing still ahead is lost"
                  % len(live_map))
        else:
            print("  empty map, and the live copy is empty too, so nothing is lost")

    if not live:
        return True, "no live copy to compare against"

    lts = str(live.get("generated_at", ""))[:19]
    nts = str(new.get("generated_at", ""))[:19]
    if lts and nts and nts < lts:
        return False, ("the Gist copy is NEWER (%s) than this run (%s) — refusing"
                       % (lts, nts))

    lost = sorted(future(live_map) - set(nn))
    print("  live on Gist : %s   %d entries (%d still ahead)"
          % (lts or "?", len(live.get("earnings", {})), len(future(live.get("earnings", {})))))
    print("  this run     : %s   %d entries (%d still ahead)"
          % (nts or "?", len(nn), len(future(nn))))
    if lost:
        return False, ("would DROP %d name(s) that have NOT reported yet: %s"
                       % (len(lost), ", ".join(lost[:12])))
    return True, "no future-dated name is lost"


def push(text, token):
    data = json.dumps({"files": {GIST_FILE: {"content": text}}}).encode()
    req = urllib.request.Request(GIST_API, data=data, method="PATCH", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "earnings-autorefresh",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            got = json.load(r)["files"][GIST_FILE]["content"]
    except urllib.error.HTTPError as e:
        hint = {401: "token invalid or expired",
                403: "token lacks Gist write permission",
                404: "token cannot see this Gist (wrong account?)"}.get(e.code, e.reason)
        raise RuntimeError("push failed HTTP %s — %s" % (e.code, hint))
    if got != text:
        raise RuntimeError("push reported success but the Gist does not match")


def notify(msg):
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        return
    try:
        req = urllib.request.Request(
            hook, data=json.dumps({"content": msg[:1900]}).encode(),
            headers={"Content-Type": "application/json",
                     "User-Agent": "earnings-autorefresh"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print("  (Discord notify failed: %s)" % e)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=LOOKAHEAD_DAYS)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and guard, but push nothing")
    ap.add_argument("--force", action="store_true",
                    help="push even if the guard objects")
    args = ap.parse_args()

    raw = fetch(args.days)
    blob = build(raw)
    text = json.dumps(blob, separators=(",", ":"))

    n = len(blob["earnings"])
    if n == 0:
        other = {}
        for e in raw.get("data", []):
            other[e.get("event_type")] = other.get(e.get("event_type"), 0) + 1
        print("0 results in Sensibull's window; other events returned: %s"
              % (", ".join("%d %s" % (c, t) for t, c in sorted(other.items()))
                 or "none"))
        confirm_results_type(args.days)
        print("  Sensibull still accepts '%s' as an event type, so this is a "
              "quiet calendar, not an API change" % RESULTS_TYPE)
    by_date = {}
    for sym, d in blob["earnings"].items():
        by_date.setdefault(d, []).append(sym)
    print("fetched %d confirmed results  (window %s -> %s)"
          % (n, blob["window_from"], blob["window_to"]))
    for d in sorted(by_date)[:8]:
        print("    %s  %s" % (d, ", ".join(sorted(by_date[d]))))
    if len(by_date) > 8:
        print("    ... and %d more dates" % (len(by_date) - 8))

    live = read_live()
    ok, why = guard(blob, live, args.force)
    print("  guard: %s" % why)

    if not ok and not args.force:
        raise RuntimeError(why)
    if not ok:
        print("  !! guard overridden by --force")

    if args.dry_run:
        print("\n--dry-run: nothing pushed.")
        return

    token = os.environ.get("GIST_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GIST_TOKEN is not set — cannot push")

    push(text, token)
    print("\npushed %s (%d bytes) -> verified" % (GIST_FILE, len(text)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        msg = "earnings-autorefresh FAILED: %s" % exc
        print(msg, file=sys.stderr)
        notify(":warning: " + msg)
        sys.exit(1)
