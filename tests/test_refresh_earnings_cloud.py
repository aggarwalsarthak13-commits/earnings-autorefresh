#!/usr/bin/env python3
"""Offline tests for scripts/refresh_earnings_cloud.py (no network).

    python3 tests/test_refresh_earnings_cloud.py
"""
import datetime as dt
import io
import os
import sys
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import refresh_earnings_cloud as r  # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


TODAY = dt.date.today()


def d(days):
    return (TODAY + dt.timedelta(days=days)).isoformat()


def stamp(minutes=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


def blob(earnings, minutes=0):
    return {"generated_at": stamp(minutes), "window_from": d(-1),
            "window_to": d(2), "earnings": earnings}


def quiet(fn):
    with redirect_stdout(io.StringIO()):
        return fn()


# --- guard: the empty-map cases --------------------------------------------
# The 2026-11-17/18 false alarm: 8 live names, all already reported.
past8 = {s: d(-5) for s in ["ABB", "EICHERMOT", "GRASIM", "INOXWIND", "TMPV"]}
past8.update({s: d(-4) for s in ["ASHOKLEY", "KAYNES", "PATANJALI"]})
ok, why = quiet(lambda: r.guard(blob({}), blob(past8, -60 * 24 * 2), False))
check("empty map passes when every live name has already reported", ok)

live = dict(past8, SWIGGY=d(3))
ok, why = quiet(lambda: r.guard(blob({}), blob(live, -60), False))
check("empty map refused when a live name is still due", not ok)
check("  ...and the refusal names it", "SWIGGY" in why)

ok, why = quiet(lambda: r.guard(blob({}), blob({"X": d(0)}, -60), False))
check("a name reporting TODAY counts as still due", not ok)

ok, why = quiet(lambda: r.guard(blob({}), None, False))
check("empty map with no readable live copy passes", ok)

ok, why = quiet(lambda: r.guard(blob({}), blob({}, -60), False))
check("empty over empty passes", ok)

ok, why = quiet(lambda: r.guard(blob({}), blob({"BAD": "not-a-date"}, -60), False))
check("a malformed live date does not crash the guard", ok)


# --- guard: unchanged behaviour on non-empty maps ---------------------------
ok, why = quiet(lambda: r.guard(blob({"A": d(5)}), blob({"A": d(5), "B": d(6)}, -60), False))
check("dropping a future-dated name is still refused", not ok and "B" in why)

ok, why = quiet(lambda: r.guard(blob({"A": d(5)}), blob({"A": d(5), "OLD": d(-2)}, -60), False))
check("dropping an already-reported name still passes", ok)

ok, why = quiet(lambda: r.guard(blob({"A": d(5)}), blob({"A": d(5)}, +60), False))
check("a NEWER Gist copy is still refused", not ok and "NEWER" in why)


# --- main(): the empty path, end to end with the network faked -------------
def run_main(raw, live, confirm_raises=None):
    pushed = []
    calls = {"confirm": 0}

    def fake_confirm(days):
        calls["confirm"] += 1
        if confirm_raises:
            raise RuntimeError(confirm_raises)

    saved = (r.fetch, r.read_live, r.push, r.confirm_results_type, sys.argv,
             os.environ.get("GIST_TOKEN"))
    r.fetch = lambda days: raw
    r.read_live = lambda: live
    r.push = lambda text, token: pushed.append(text)
    r.confirm_results_type = fake_confirm
    sys.argv = ["refresh_earnings_cloud.py"]
    os.environ["GIST_TOKEN"] = "test-token"
    err = None
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            r.main()
    except Exception as e:
        err = str(e)
    finally:
        (r.fetch, r.read_live, r.push, r.confirm_results_type, sys.argv, tok) = saved
        if tok is None:
            os.environ.pop("GIST_TOKEN", None)
        else:
            os.environ["GIST_TOKEN"] = tok
    return pushed, calls["confirm"], err, out.getvalue()


# Shaped like Sensibull's real 2026-11-18 answer: dividends, no results.
quiet_raw = {"from_date": d(-1), "to_date": d(2), "data": [
    {"event_type": "dividend", "trading_symbol": "PAGEIND", "date": d(0)},
    {"event_type": "dividend", "trading_symbol": "OIL", "date": d(2)},
]}
pushed, nconf, err, out = run_main(quiet_raw, blob(past8, -60 * 24))
check("quiet calendar: pushes once", len(pushed) == 1 and err is None)
check("  ...pushes an empty earnings map", '"earnings":{}' in (pushed or [""])[0])
check("  ...checked the event type exactly once", nconf == 1)
check("  ...logs the dividends it saw", "2 dividend" in out)

pushed, nconf, err, out = run_main(
    quiet_raw, blob(past8, -60 * 24),
    confirm_raises="0 results returned AND Sensibull no longer accepts 'stock_results'")
check("renamed event type: nothing pushed", pushed == [])
check("  ...fails with the API-change message", err and "no longer accepts" in err)

pushed, nconf, err, out = run_main(quiet_raw, blob(dict(past8, SWIGGY=d(3)), -60))
check("empty map with a name still due: nothing pushed", pushed == [] and "SWIGGY" in (err or ""))

busy_raw = {"from_date": d(-1), "to_date": d(30), "data": [
    {"event_type": "stock_results", "trading_symbol": "TCS", "date": d(10)},
    {"event_type": "dividend", "trading_symbol": "OIL", "date": d(2)},
]}
pushed, nconf, err, out = run_main(busy_raw, blob(past8, -60))
check("normal day: pushes, and skips the extra event-type call",
      len(pushed) == 1 and nconf == 0 and '"TCS"' in pushed[0])


print()
print("%d failed" % len(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
