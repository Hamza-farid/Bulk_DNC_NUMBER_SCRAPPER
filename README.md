# Bulk DNC / TCPA Checker — infolookup.site

A fresh rebuild of the number-range DNC checker: Python backend + Streamlit
frontend. Generates phone numbers in bulk from a 3-part range picker
(area code / exchange range / line-number range), checks each one against
`infolookup.site`'s TCPA lookup API, and lets you download the results as CSV.

## How the site's auth actually works (read this first)

This was reverse-engineered directly from the site's own JS (`test2.js`,
`backend.js`) and confirmed by live testing:

- The site calls `GET /lookup-token.php` to get a short-lived "`_t`" token,
  then uses it on `GET /api/tcpa?x=<number>&_t=<token>&pi=1`.
- **`/lookup-token.php` is public and unauthenticated** — no login, no
  Cloudflare/Turnstile challenge. Anyone can request a token.
- The token is a base64 blob of `<unix timestamp>|<requesting IP>.<hash>` —
  it's cryptographically bound to whichever IP address requested it, and
  expires after ~15 minutes.
- Calling `/api/tcpa` with a token minted from a **different IP** than the
  one making that call returns `HTTP 403 {"status":"error","message":"Access
  denied."}` — even though the token itself is fresh. This is almost
  certainly why the old tool "sometimes just didn't work": if a token gets
  captured once (e.g. copy-pasted) and then reused from a different
  machine/proxy/connection, it silently fails.

**The practical upshot: you do not need to paste any token by hand.** This
app mints its own token automatically via `/lookup-token.php` and reuses it
for every request that shares the same outbound connection, refreshing it
before it expires (see `backend/session_manager.py`). A manual override
field still exists in the sidebar as a fallback in case the site ever locks
that endpoint down, but as of writing you'll never need it.

**Real API response shape** (confirmed live):
```json
{"status":"ok","phone":"8065551234","listed":"No","type":"No","state":"TX","ndnc":"No","sdnc":"No"}
```
A number is bucketed as **DNC** if `ndnc == Yes`, `sdnc == Yes`, or
`listed == Yes`; otherwise **Valid**. (There's no separate
litigator/blacklist field from this endpoint — the site's own frontend
code only derives those from a field this data source never actually
populates, so they're effectively folded into `listed`.)

## Project structure

```text
dnc_bulk_checker/
├── app.py                     # Streamlit UI — the only file you "run"
├── backend/
│   ├── config.py               # site URLs, headers, timeouts
│   ├── logger_setup.py         # logging to console + logs/app.log + in-app debug panel
│   ├── number_builder.py       # builds the number list from the 3 ranges
│   ├── session_manager.py      # auto-mints & caches the per-IP lookup token
│   ├── proxy_manager.py        # optional proxy pool
│   ├── csv_store.py            # buffered CSV writer + resume support
│   └── checker_engine.py       # the multi-threaded checker itself
├── data/                       # output CSVs land here (auto-created)
├── logs/                       # app.log lives here (auto-created)
├── proxies.txt                 # optional, one "IP PORT" per line
└── requirements.txt
```

## 1. Install & run locally

```bash
cd dnc_bulk_checker
pip install -r requirements.txt
streamlit run app.py
```

It opens at `http://localhost:8501`. Click **Test connection** in the
sidebar first — it mints a token and fires one real request, so you know
immediately if everything's reachable before starting a bulk run.

## 2. Choose the number range

Phone numbers are NPA-NXX-XXXX (area code, exchange, line number). Instead
of one flat range, you pick each segment separately:

- **Area code** — the fixed 3-digit prefix, e.g. `806`.
- **Exchange range** — the next 3 digits, as a start–end range, e.g. `549`–`560`.
- **Line number range** — the last 4 digits, as a start–end range, e.g. `0000`–`9999`.

The app multiplies these out for you and shows the total count live, so
you can see exactly how many numbers a given range produces before
starting. Start narrow to test things, then widen once you've confirmed
things are working well.

## 3. Performance settings

- **Parallel threads** — since every thread on the "direct" (no-proxy)
  connection shares the same token, this is now mostly limited by how
  aggressively the site rate-limits, not by session juggling. Start around
  20–40 and watch the Debug logs panel for 429s before pushing higher.
- **Chunk size** — numbers are processed in batches; between batches the
  engine sleeps for the configured seconds to ease off the API.
- **Sleep between chunks** — the pause duration mentioned above.

## 4. Run it

Click **Start**. You can **Pause**/**Resume** at any time, and **Stop** to
end the run early — whatever's already been written to CSV stays there.
**Reset data** clears all three output CSVs (only enabled while nothing is
running).

Progress, live speed, ETA, and the last dozen results are shown while a run
is active. The bottom **Debug logs** panel shows the last 200 log lines
(the same ones going to `logs/app.log`) — check it first if something looks
stuck or you're seeing a lot of errors.

## 5. Resume support

Every number written to any of the three output CSVs is remembered. If you
stop a run and start a new one over a range that overlaps, already-checked
numbers are automatically skipped.

## 6. Download results

Use the **Valid / DNC / Not found / All** buttons above the table to filter,
then **Download \<filter\> CSV** to export exactly that view. The raw CSVs
also live directly in `data/` if you'd rather grab them from disk.

## 7. Proxies (optional, and how tokens interact with them)

Because the token is IP-bound, using proxies means each proxy needs its
**own** token, minted through itself — the app already does this
automatically (`session_manager.py` keys everything by proxy address). If
you add proxies to `proxies.txt`, no other setup is needed; just know that
the first request through a new proxy will always cost one extra round-trip
to mint that proxy's token.

## 8. Deploying to Streamlit Community Cloud

1. Push this `dnc_bulk_checker/` folder to a GitHub repo (a `.gitignore`
   is included so the `data/` CSVs and `logs/` don't get committed).
2. On [share.streamlit.io](https://share.streamlit.io), create a new app,
   point it at the repo, and set the main file to `app.py`.
3. No secrets are required — everything auth-related happens automatically
   at runtime.
4. **Important limitation**: Streamlit Cloud's filesystem is ephemeral and
   the app can sleep after inactivity — a long bulk run left unattended may
   get interrupted, and `data/`/`logs/` reset on redeploy. For genuinely
   large bulk runs, run locally (or on a small always-on VM) and use the
   Cloud deployment mainly for demos / smaller on-demand checks. Either
   way, download your CSVs as soon as a run finishes rather than leaving
   them sitting in `data/`.

## Debugging checklist

If checks are failing or nothing is happening:

1. Open the **Debug logs** expander at the bottom of the page first.
2. Click **Test connection** in the sidebar — it mints a token and fires
   one real request, telling you directly whether it worked, got rate
   limited (429), or was rejected (403 — normally means the token got
   invalidated and needs a re-mint, which happens automatically on the
   next request).
3. If you see a burst of repeated 403s during a run, that's the app
   catching a token-vs-IP mismatch and automatically re-minting — it's
   expected occasionally and self-heals. If it happens on *every* request,
   your network path may have an unstable/rotating outbound IP (some
   corporate VPNs or certain cloud NAT setups do this) — try running from
   a plain home/office connection or a standard VPS instead.
4. If you see repeated 429s, lower the thread count and/or increase the
   sleep-between-chunks setting.
