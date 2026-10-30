# earnings-autorefresh

Rebuilds `earnings.json` from Sensibull every morning at **08:15 IST** and pushes
it into the F&O Monitor Gist. No Mac, no paste, no chat session. If you forget to
refresh locally, the dashboard's Earnings column stays current anyway.

Same shape as `fade-cloud`: a cron'd GitHub Actions workflow, a token in repo
secrets, a Discord ping when it fails.

**Earnings only.** `deploy_gate.json` cannot run here — it needs the local
575-symbol OHLC cache and a Kite token that expires every day. That stays on the
Mac as `python3 refresh_gate.py`.

---

## Cost

**₹0.** Public repos get unlimited standard-runner minutes. One run a day,
roughly 25–40 seconds each, is free and does not touch the 2,000 min/month
private-repo allowance that `fade-cloud` is already eating (~1,800 of it).

That is the main reason this is its own public repo rather than another workflow
inside `fade-cloud`: if `fade-cloud` ever crosses 2,000 minutes, **every**
scheduled workflow in your private repos stops, and you would lose earnings too.
Separate repo, separate (uncapped) meter.

---

## Setup — once, about 5 minutes, all in the browser

### 1. Create the repo

github.com → New repository → name `earnings-autorefresh` → **Public** → Create.

Public is safe here because the repo contains **no secret and no private data**:

| In the repo | Why it's fine in public |
|---|---|
| The Gist ID | The Gist is already world-readable — `curl` on the raw URL returns 200 with no auth. You decided that deliberately when you put the S1–S4 thresholds in it. |
| The Sensibull endpoints | Anonymous API, no account, no key. |
| `GIST_TOKEN` | **Not in the repo.** It lives in Settings → Secrets, is masked in logs, and GitHub will not show it again after you paste it. |

The workflow triggers on `schedule` and `workflow_dispatch` only. There is no
`pull_request` trigger, so a stranger forking the repo cannot get a run that can
see your secret. Don't add one.

### 2. Add these two files, paths exactly as written

```
.github/workflows/refresh-earnings.yml
scripts/refresh_earnings_cloud.py
```

Easiest route: "Add file" → "Create new file" → paste the path including the
slashes into the filename box, paste the contents, commit.

### 3. Add the token

Repo → Settings → Secrets and variables → Actions → New repository secret.

- Name: `GIST_TOKEN`
- Value: the gist token you regenerated after the screenshot leak

Fine-grained token, **Gists: read and write**, nothing else. That is the whole
blast radius if it ever leaks again: your gists, no repos, no account settings.

### 4. Add the Discord webhook (optional but do it)

Same page, another secret:

- Name: `DISCORD_WEBHOOK_URL`
- Value: the webhook you already use for the fade scans

Without it, a failure is a silent red tick in a tab you don't check. With it, you
get one line in Discord saying what broke. The script never posts on success —
no daily noise.

### 5. Test before trusting it

Actions tab → "Refresh earnings.json" → Run workflow → tick **dry_run** → Run.

The log should end with something like:

```
fetched 83 confirmed results  (window 2026-10-29 -> 2026-11-13)
  live on Gist : 2026-10-27T15:51:56   115 entries (64 still ahead)
  this run     : 2026-10-30T04:44:47   83 entries (71 still ahead)
  guard: no future-dated name is lost
--dry-run: nothing pushed.
```

Then run it again **without** dry_run. It should finish with
`pushed earnings.json (N bytes) -> verified`. Reload the dashboard and check the
Earnings column shows POLICYBZR and POWERGRID.

After that it runs itself. GitHub disables cron on repos with no activity for 60
days — pushing a commit or clicking Run workflow resets that clock, and a daily
schedule on an active account generally doesn't hit it.

---

## The guard, and why it is different from your Mac's

`push_gist.py` on the Mac refuses a push when the new file has fewer **total**
entries than the live one. That check is wrong for earnings, and it will block
your next local refresh.

Every run asks Sensibull for a window starting *today*, so names that have
already reported drop off the back. On 2026-10-30 a perfectly correct refresh
returned **83** entries against a live file of **115** — the total check refuses
it, while in fact zero future-dated names were lost and the new file had **more**
of them (71 vs 64).

So this script checks the thing that actually matters:

> Does this push drop any symbol whose earnings date is **today or later**?

A name that already reported falling off is housekeeping. A name still due to
report disappearing is the dangerous case — that is a position whose earnings
warning silently vanishes from the dashboard.

Four other refusals, all tested:

| Refuses | Why |
|---|---|
| New map is empty while the live one isn't | Sensibull outage would blank the whole Earnings column |
| A future-dated name is lost | The real failure mode (above) |
| The Gist copy is newer than this run | Another writer got there first; don't clobber it |
| Push reported success but the Gist content doesn't match | Re-reads after writing, every time |

`--force` overrides the guard. `--dry-run` skips the push entirely.

**You still need to fix the Mac copy.** `push_gist.py` has the total-count guard
and will refuse today's legitimate refresh. Either port the forward-looking check
into it, or — simpler — stop pushing earnings from the Mac now that this runs in
the cloud, and let `refresh_earnings.py --no-push` just write the local copy.

---

## Running it by hand

```bash
export GIST_TOKEN="$(cat ~/Desktop/Sakshi/niftycheck/gist_token.txt)"
python3 scripts/refresh_earnings_cloud.py --dry-run   # look first
python3 scripts/refresh_earnings_cloud.py             # then push
```

Note the path: the token lives in `niftycheck/`, not `Sakshi/`. The docstring in
your `push_gist.py` says `~/Desktop/Sakshi/gist_token.txt`, which is wrong — its
own code reads `HERE.parent / "gist_token.txt"`, i.e. `niftycheck/`. The code is
right, the comment is stale.

Stdlib only, no `pip install`. The token is read from the environment, never from
an argument — an argument lands in your shell history and in `ps`.

---

## If it breaks

| Symptom | Cause | Fix |
|---|---|---|
| `no access_token cookie` | Sensibull changed the identify endpoint | Check the two URLs at the top of the script against web.sensibull.com's network tab |
| HTTP 403 from the events call | The `Origin: https://web.sensibull.com` header is the whole trick — if it's dropped, Sensibull rejects you | Don't remove the `HEADERS` dict |
| `push failed HTTP 401` | Token expired or revoked | New fine-grained token, Gists read+write, update the secret |
| `push failed HTTP 404` | Token belongs to a different GitHub account than the Gist | Regenerate under `aggarwalsarthak13-commits` |
| `would DROP n name(s)` | Either a genuine Sensibull regression, or a name genuinely got cancelled | Read the names in the log. If the drop is real, `--force` once |
| Nothing runs at all | Cron disabled after 60 days idle, or Actions turned off for the repo | Actions tab will say so; click Run workflow |

---

## Rotating the token

Revoke at GitHub → Settings → Developer settings → Personal access tokens, issue
a new one with Gists read+write, update the `GIST_TOKEN` secret. Nothing else
changes. Never commit it to the repo — this repo is public.

---

## What still needs you

- `deploy_gate.json` — `python3 refresh_gate.py` on the Mac (needs the Kite token).
- `option-monitor.js` — code changes only, `preflight_gist.py` first, pasted
  deliberately. Nothing automated ever writes that file.
