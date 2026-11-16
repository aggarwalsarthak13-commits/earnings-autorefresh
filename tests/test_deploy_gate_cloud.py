#!/usr/bin/env python3
"""Offline tests for scripts/deploy_gate_cloud.py (no network).

    python3 tests/test_deploy_gate_cloud.py
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import deploy_gate_cloud as g  # noqa: E402

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# --- corporate actions ------------------------------------------------------
f = g.corporate_action_factor
check("ordinary 5% drop ignored", f(0.95) is None)
check("20% circuit open (exactly 0.80) ignored", f(0.80) is None)
check("10% band open (0.90) ignored", f(0.90) is None)
check("1:1 bonus snaps to 1/2", f(0.4987) == 0.5)
check("1:5 split with +4% move snaps to 1/5", f(0.2089) == 0.2)
check("1:3 bonus opening weak (CUB 0.7687) snaps to 3/4", f(0.7687) == 0.75)
check("demerger uses the gap itself", abs(f(0.6) - 0.6) < 1e-9)
check("unexplained jump up left alone", f(1.6) is None)


# --- update / append / reading on synthetic days -----------------------------
def fake_cm(prices):
    cm = {"S%03d" % i: (p, p, "INE%07d" % i) for i, p in enumerate(prices)}
    for k in range(1000):            # padding so the truncation check passes
        cm["PAD%04d" % k] = (10.0, 10.0, "")
    return cm


days = []
d = dt.date(2025, 1, 1)
while len(days) < 260:
    if d.weekday() < 5:
        days.append(d)
    d += dt.timedelta(days=1)
holiday = days[100]
data = {}
for n, day in enumerate(days):
    if day == holiday:
        continue
    # 450 flat stocks at 100; 50 stocks ramping up; stock S000 splits 1:2 on day 200
    prices = [100.0] * 450 + [100.0 + n * 0.1] * 50
    if n >= 200:
        prices[0] = 50.0
    data[day] = (fake_cm(prices), 20000.0 + n)


def fake_fetch_day(day):
    if day in data:
        return day, data[day][0], data[day][1]
    raise g.NotPublished("no NSE files for %s" % day)


g.fetch_day = fake_fetch_day
s = g.empty_state()
added = g.update(s, days[-1] + dt.timedelta(days=1))
check("all trading days added, holiday skipped", added == len(days) - 1 and holiday.isoformat() not in s["days"])
check("weekends between trading days skipped", all(dt.date.fromisoformat(x).weekday() < 5 for x in s["days"]))
check("split detected", any(a[1] == "S000" and a[2] == 0.5 for a in s["adjustments"]))
h = [x for x in s["close"]["S000"] if x is not None]
check("history before split scaled to 50", abs(h[0] - 50.0) < 1e-6 and abs(h[-1] - 50.0) < 1e-6)

universe = ["S%03d" % i for i in range(500)]
r, n200 = g.reading(s, universe)
check("median of flat universe is 0", r["med_stock_vs200"] == 0.0 and r["level"] == "" and r["action"] == "hold")
check("all 500 stocks have 200d history", n200 == 500 and r["universe"] == 500)
check("nifty at its high -> dd 0", r["nifty_dd"] == 0.0)
check("to_next walks to watch", r["to_next"] == 8.0 and r["next_label"] == "watch")
check("json is strict", json.loads(json.dumps(r, allow_nan=False)) == r)

# incremental: a later run adds only new days, and a not-yet-published today stops cleanly
before = len(s["days"])
nxt = days[-1] + dt.timedelta(days=3)
while nxt.weekday() >= 5:
    nxt += dt.timedelta(days=1)
data[nxt] = (fake_cm([90.0] * 500), 18000.0)
added2 = g.update(s, nxt + dt.timedelta(days=1))
check("incremental run adds just the new day", added2 == 1 and len(s["days"]) == before + 1)
r2, _ = g.reading(s, universe)
check("a broad 10% fall reads about -10% and WATCH", -11 < r2["med_stock_vs200"] < -9 and r2["level"] == "WATCH")
check("nifty drawdown from 252d high", r2["nifty_dd"] == round((18000.0 / max(x for x in s["nifty"][-252:] if x) - 1) * 100, 1))


# a real download error must stop the run, never skip the day
def broken(day):
    raise RuntimeError("HTTP 503")


g.fetch_day = broken
try:
    g.update(s, nxt + dt.timedelta(days=5))
    check("download error raises", False)
except RuntimeError:
    check("download error raises", True)

# too few stocks with history -> refuse
try:
    g.reading(s, universe[:300])
    check("thin universe refused", False)
except RuntimeError:
    check("thin universe refused", True)

print("\n%d failure(s)" % len(FAILS))
sys.exit(1 if FAILS else 0)
