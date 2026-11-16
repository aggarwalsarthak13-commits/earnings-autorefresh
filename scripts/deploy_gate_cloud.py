#!/usr/bin/env python3
"""
deploy_gate_cloud.py: compute the lump-sum deploy gate from NSE's free
end-of-day files and push deploy_gate.json into the F&O Monitor Gist.

This is the cloud twin of the Mac's refresh_gate.py / deploy_gate.py. The rule
and the output file are the same. Only the price source differs:

    Mac    Kite daily candles (needs the morning Kite login, Mac awake)
    cloud  NSE CM bhavcopy + NSE index closes (no login, no Mac)

The gate is ONE number: the MEDIAN Nifty-500 stock's distance from its OWN
200-day moving average. Bands (24-month hold):

    above -8%      HOLD      no deploy
    -8 to -12%     WATCH     informational only
    -12 to -18%    LEVEL B   start some
    -18 to -24%    LEVEL A   deploy most
    below -24%     LEVEL A+  ALL IN

nifty_dd is the Nifty 50's drawdown from its trailing 252-day high, exactly as
deploy_gate.py computes it.

Price history lives in a small state file (--state). On a cold start it is
rebuilt from NSE's archive (about 320 trading days, a few minutes). After that
each run downloads only the days it is missing.

Splits, bonuses and demergers: NSE's bhavcopy is NOT adjusted for them (on
HDFC Bank's 1:1 bonus day the "previous close" still read 1,964 against a 973
close). So an opening gap beyond -21%/+35% is treated as a corporate action:
it is snapped to the nearest standard ratio (1/2, 1/5, 2/3 ...) when one fits
within 6%, otherwise the opening gap itself is used, and the stock's stored
history is scaled by that factor. Without this, a split stock would sit far
below its 200 DMA for ten months and drag the median down.

    python3 scripts/deploy_gate_cloud.py               # update, compute, push
    python3 scripts/deploy_gate_cloud.py --dry-run     # update, compute, push nothing
    python3 scripts/deploy_gate_cloud.py --force       # push even if as_of is unchanged

Environment
    GIST_TOKEN            required to push. Fine-grained token, Gists read+write.
    DISCORD_WEBHOOK_URL   optional. A failure posts one message here.
"""
import csv
import datetime as dt
import gzip
import io
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NSE_CM = ("https://nsearchives.nseindia.com/content/cm/"
          "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip")
NSE_IDX = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv"
NSE_N500 = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"

GIST_ID = "c5eb3c6a51b620bec116849afd83cd70"
GIST_FILE = "deploy_gate.json"
GIST_RAW = ("https://gist.githubusercontent.com/aggarwalsarthak13-commits/"
            + GIST_ID + "/raw/" + GIST_FILE)
GIST_API = "https://api.github.com/gists/" + GIST_ID

KEEP_DAYS = 320          # trading days kept in state (200 DMA + 252d Nifty high + slack)
SEED_CAL_DAYS = 480      # calendar days fetched on a cold start (~325 trading days)
SERIES = ("EQ", "BE", "BZ", "RR")   # RR = REITs, which are in the Nifty 500 list
MIN_N200 = 400           # refuse if fewer Nifty-500 stocks have a full 200-day history
# Opening gaps outside this band = corporate action. 0.79 sits just below the 20%
# lower circuit: opens at exactly 0.80 / 0.90 of the last close are real circuit
# hits (TARIL, PIRAMALFIN), while 1:4 bonuses open just under 0.80 (CUB 0.769,
# BRIGADE 0.777).
GAP_LO, GAP_HI = 0.79, 1.35
SNAP_TOL = 0.06          # how close a gap must be to a standard ratio to snap to it
# new price / old price for common splits, bonuses and split+bonus combinations
STD_RATIOS = sorted({1/2, 1/3, 1/4, 1/5, 1/6, 1/8, 1/10, 1/20, 1/25, 1/50,
                     2/3, 3/4, 2/5, 3/5, 3/10, 2, 3, 4, 5, 10})

BANDS = [(-24, 'A+', 'ALL IN'), (-18, 'A', 'deploy most'),
         (-12, 'B', 'start some'), (-8, 'WATCH', 'watch only - no deploy'),
         (999, '', 'hold')]
LEVELS = (-8, -12, -18, -24)
LEVEL_LABEL = {-8: 'watch', -12: 'Level B', -18: 'Level A', -24: 'Level A+'}


# --------------------------------------------------------------------- download
class NotPublished(Exception):
    """NSE answered 404: no file for that date (holiday, or not out yet)."""


def http_get(url, tries=4):
    cache = os.environ.get("GATE_DL_CACHE")          # local testing only
    if cache:
        fn = os.path.join(cache, url.rsplit("/", 1)[1])
        if os.path.exists(fn):
            with open(fn, "rb") as f:
                return f.read()
        if os.path.exists(fn + ".404"):
            raise NotPublished(url)
        try:
            data = http_get_net(url, tries)
        except NotPublished:
            open(fn + ".404", "w").close()
            raise
        with open(fn, "wb") as f:
            f.write(data)
        return data
    return http_get_net(url, tries)


def http_get_net(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotPublished(url)
            last = e
        except Exception as e:  # timeouts, resets
            last = e
        time.sleep(2 + 3 * i)
    raise RuntimeError("download failed after %d tries: %s (%s)" % (tries, url, last))


def fetch_cm(d):
    """{symbol: (open, close, isin)} for one trading day, or NotPublished."""
    raw = http_get(NSE_CM.format(ymd=d.strftime("%Y%m%d")))
    z = zipfile.ZipFile(io.BytesIO(raw))
    out = {}
    with z.open(z.namelist()[0]) as f:
        for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace")):
            if r.get("SctySrs") not in SERIES:
                continue
            try:
                c = float(r["ClsPric"])
                o = float(r["OpnPric"] or 0)
            except (TypeError, ValueError):
                continue
            if c > 0:
                out[r["TckrSymb"].strip()] = (o if o > 0 else c, c, (r.get("ISIN") or "").strip())
    if len(out) < 1000:
        raise RuntimeError("bhavcopy for %s looks truncated (%d rows)" % (d, len(out)))
    return out


def fetch_nifty(d):
    raw = http_get(NSE_IDX.format(dmy=d.strftime("%d%m%Y"))).decode("utf-8", "replace")
    for r in csv.DictReader(io.StringIO(raw)):
        if (r.get("Index Name") or "").strip().lower() == "nifty 50":
            return float(r["Closing Index Value"])
    raise RuntimeError("Nifty 50 row missing from index file for %s" % d)


def fetch_day(d):
    """(date, cm, nifty) or (date, None, None) when NSE has no file for that date."""
    try:
        cm = fetch_cm(d)
    except NotPublished:
        cm = None
    try:
        nifty = fetch_nifty(d)
    except NotPublished:
        nifty = None
    if cm is None and nifty is None:
        raise NotPublished("no NSE files for %s" % d)
    if cm is None or nifty is None:
        # One file exists and the other doesn't. That is either a publishing lag
        # (equity file out, index file not yet) or a real problem; never record
        # the day half-filled.
        raise NotPublished("only one of the two NSE files exists for %s" % d)
    return d, cm, nifty


def fetch_universe(state):
    try:
        raw = http_get(NSE_N500).decode("utf-8", "replace")
        syms = [r["Symbol"].strip() for r in csv.DictReader(io.StringIO(raw))
                if r.get("Symbol") and not r["Symbol"].startswith("DUMMY")]
        if len(syms) >= 450:
            state["universe"] = syms
            state["universe_at"] = dt.date.today().isoformat()
            return syms
        print("  warn: Nifty 500 list had only %d rows, using the stored one" % len(syms))
    except Exception as e:
        print("  warn: Nifty 500 list download failed (%s), using the stored one" % e)
    if not state.get("universe"):
        raise RuntimeError("no Nifty 500 constituent list available")
    return state["universe"]


# --------------------------------------------------------------------- state
def empty_state():
    return {"version": 1, "days": [], "nifty": [], "close": {}, "isin": {},
            "adjustments": [], "universe": [], "universe_at": None}


def load_state(path):
    if path and os.path.exists(path):
        with gzip.open(path, "rt") as f:
            s = json.load(f)
        print("state: %d trading days, %s .. %s, %d symbols"
              % (len(s["days"]), s["days"][0] if s["days"] else "-",
                 s["days"][-1] if s["days"] else "-", len(s["close"])))
        return s
    print("state: none, cold start")
    return empty_state()


def save_state(s, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt") as f:
        json.dump(s, f, separators=(",", ":"))
    os.replace(tmp, path)


def corporate_action_factor(gap):
    """Scaling factor for history when the open gaps by `gap` (open / last close),
    or None for an ordinary move."""
    if GAP_LO <= gap <= GAP_HI:
        return None
    best = min(STD_RATIOS, key=lambda r: abs(math.log(gap / r)))
    if abs(math.log(gap / best)) <= math.log(1 + SNAP_TOL):
        return best
    if gap < GAP_LO:
        return gap          # demerger or other non-standard event: use the gap itself
    return None             # an unexplained jump up is left alone


def append_day(s, d, cm, nifty):
    """Add one trading day. Applies corporate-action scaling and symbol renames."""
    n = len(s["days"])
    by_isin = {v: k for k, v in s["isin"].items()}
    for sym, (o, c, isin) in cm.items():
        if sym not in s["close"] and isin and isin in by_isin:
            old = by_isin[isin]
            if old in s["close"] and old not in cm:   # renamed: carry history over
                s["close"][sym] = s["close"].pop(old)
                s["isin"].pop(old, None)
                s["adjustments"].append([d.isoformat(), sym, "rename from " + old])
        hist = s["close"].setdefault(sym, [None] * n)
        last = next((x for x in reversed(hist) if x is not None), None)
        f = corporate_action_factor(o / last) if last else None
        if f:
            s["close"][sym] = hist = [None if x is None else round(x * f, 4) for x in hist]
            s["adjustments"].append([d.isoformat(), sym, round(f, 6), round(o / last, 4)])
        hist.append(c)
        if isin:
            s["isin"][sym] = isin
    for sym, hist in s["close"].items():
        if len(hist) == n:
            hist.append(None)
    s["days"].append(d.isoformat())
    s["nifty"].append(nifty)


def trim(s):
    k = len(s["days"]) - KEEP_DAYS
    if k <= 0:
        return
    s["days"] = s["days"][k:]
    s["nifty"] = s["nifty"][k:]
    for sym in list(s["close"]):
        h = s["close"][sym][k:]
        if all(x is None for x in h):
            del s["close"][sym]
            s["isin"].pop(sym, None)
        else:
            s["close"][sym] = h
    cut = s["days"][0]
    s["adjustments"] = [a for a in s["adjustments"] if a[0] >= cut]


def update(s, today):
    """Download every day after the last stored day, up to today.

    Weekends are included on purpose: NSE holds special sessions on some of them
    (Budget day, Muhurat trading). Missing such a session would make the next
    day's adjusted previous close look like a corporate action on every stock."""
    start = (dt.date.fromisoformat(s["days"][-1]) + dt.timedelta(days=1)) if s["days"] \
        else today - dt.timedelta(days=SEED_CAL_DAYS)
    dates = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    if not dates:
        return 0
    print("fetching %d day(s) %s .. %s" % (len(dates), dates[0], dates[-1]))
    results = {}
    errors = {}

    def one(d):
        try:
            return fetch_day(d)
        except NotPublished as e:
            return d, "missing", str(e)
        except Exception as e:
            return d, "error", str(e)

    with ThreadPoolExecutor(4) as ex:
        for d, a, b in ex.map(one, dates):
            if a == "error":
                errors[d] = b
            elif a == "missing":
                results[d] = None
                if "only one" in b:
                    results[d] = "half"
            else:
                results[d] = (a, b)

    added = 0
    for d in dates:
        if d in errors:
            # Stop at the first real failure: never append later days past a hole.
            raise RuntimeError("could not download %s: %s" % (d, errors[d]))
        r = results[d]
        later = any(isinstance(results.get(x), tuple) for x in dates if x > d)
        if r is None or r == "half":
            if r == "half" and later:
                raise RuntimeError("%s has only one of the two NSE files but later days exist" % d)
            if not later:
                break            # today / not yet published: try again next run
            continue             # a holiday between two trading days
        cm, nifty = r
        append_day(s, d, cm, nifty)
        added += 1
    return added


# --------------------------------------------------------------------- the gate
def next_band(v):
    for t in LEVELS:
        if v > t:
            return round(abs(t - v), 1), LEVEL_LABEL[t]
    return 0.0, ''


def reading(s, universe, i=None):
    """The deploy_gate.json dict for trading day index i (default: the latest)."""
    if i is None:
        i = len(s["days"]) - 1
    vals = []
    present = 0
    for sym in universe:
        h = s["close"].get(sym)
        if not h or h[i] is None:
            continue
        present += 1
        hist = [x for x in h[:i + 1] if x is not None]
        if len(hist) < 200:
            continue
        sma = sum(hist[-200:]) / 200.0
        vals.append((h[i] / sma - 1) * 100)
    if len(vals) < MIN_N200:
        raise RuntimeError("only %d Nifty-500 stocks have 200 days of history on %s (need %d)"
                           % (len(vals), s["days"][i], MIN_N200))
    v = statistics.median(vals)

    ns = [x for x in s["nifty"][max(0, i - 251):i + 1] if x is not None]
    if len(ns) < 120 or s["nifty"][i] is None:
        raise RuntimeError("not enough Nifty history for the 252-day high on %s" % s["days"][i])
    nifty_dd = round((s["nifty"][i] / max(ns) - 1) * 100, 1)

    lvl, what = next((l, w) for t, l, w in BANDS if v <= t)
    to_next, next_label = next_band(v)
    near = min(abs(v - t) for t in LEVELS)
    return dict(as_of=s["days"][i],
                generated_at=dt.datetime.now(dt.timezone.utc)
                .isoformat(timespec='seconds').replace('+00:00', 'Z'),
                med_stock_vs200=round(v, 2),
                nifty_dd=nifty_dd,
                level=lvl, action=what,
                to_next=to_next, next_label=next_label,
                warn_days=3 if near <= 4 else 7 if near <= 8 else 14,
                universe=present), len(vals)


# --------------------------------------------------------------------- gist
def read_live():
    url = GIST_RAW + "?t=" + str(int(time.time()))
    req = urllib.request.Request(url, headers={"User-Agent": "deploy-gate-cloud",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def push(text, token):
    data = json.dumps({"files": {GIST_FILE: {"content": text}}}).encode()
    req = urllib.request.Request(GIST_API, data=data, method="PATCH", headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "deploy-gate-cloud"})
    with urllib.request.urlopen(req, timeout=30) as r:
        got = json.load(r)["files"][GIST_FILE]["content"]
    if got.strip() != text.strip():
        raise RuntimeError("Gist answered with different content than was sent")


def notify(msg):
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not hook:
        return
    try:
        body = json.dumps({"content": msg[:1900]}).encode()
        urllib.request.urlopen(urllib.request.Request(
            hook, data=body, headers={"Content-Type": "application/json",
                                      "User-Agent": "deploy-gate-cloud"}), timeout=15)
    except Exception as e:
        print("  (discord notify failed: %s)" % e)


def describe(r, n200):
    lbl = ("LEVEL " + r["level"]) if r["level"] not in ("", "WATCH") else (r["level"] or "HOLD")
    extra = ("  (%.1fpp to %s)" % (r["to_next"], r["next_label"])) if r["next_label"] else ""
    return ("as of %s: median stock %+.2f%% vs its 200 DMA, Nifty %+.1f%% -> %s%s  "
            "[%d of %d stocks with 200d history]"
            % (r["as_of"], r["med_stock_vs200"], r["nifty_dd"], lbl, extra, n200, r["universe"]))


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    force = "--force" in args
    state_path = args[args.index("--state") + 1] if "--state" in args else "state/gate_state.json.gz"
    out_path = args[args.index("--out") + 1] if "--out" in args else None

    s = load_state(state_path)
    today = dt.datetime.now(IST).date()
    added = update(s, today)
    trim(s)
    print("added %d trading day(s); history now %s .. %s"
          % (added, s["days"][0], s["days"][-1]))
    universe = fetch_universe(s)
    if 0 < added <= 10:                    # incremental run: show what changed
        for a in s["adjustments"]:
            if a[0] >= s["days"][-added]:
                print("  corporate action / rename:", a)

    r, n200 = reading(s, universe)
    print(describe(r, n200))
    text = json.dumps(r, separators=(",", ":"), allow_nan=False)
    save_state(s, state_path)          # history is good even if the push is skipped
    if out_path:
        with open(out_path, "w") as f:
            f.write(text + "\n")

    live = None
    try:
        live = read_live()
        print("live on Gist: as_of %s, median %s, Nifty %s"
              % (live.get("as_of"), live.get("med_stock_vs200"), live.get("nifty_dd")))
    except Exception as e:
        print("  warn: could not read the live Gist file (%s)" % e)

    if live and live.get("as_of", "") > r["as_of"]:
        print("live file is NEWER than this run (%s > %s): not pushing"
              % (live["as_of"], r["as_of"]))
        return 0
    if live and live.get("as_of") == r["as_of"] and not force:
        print("live file already has %s: nothing to push" % r["as_of"])
        return 0
    if dry:
        print("--dry-run: would push %s" % text)
        return 0

    token = os.environ.get("GIST_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GIST_TOKEN is not set: cannot push")
    push(text + "\n", token)
    print("pushed %s -> verified" % GIST_FILE)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        msg = "⚠️ deploy gate (cloud) failed: %s" % e
        print(msg, file=sys.stderr)
        notify(msg + "\nThe dashboard keeps showing the last good reading. "
               "Re-run: GitHub → Actions → Refresh deploy_gate.json → Run workflow.")
        sys.exit(1)
