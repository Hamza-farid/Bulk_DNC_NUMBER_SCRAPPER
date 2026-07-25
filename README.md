# Bulk DNC / TCPA Checker

A fresh rebuild of the number-range DNC checker: Python backend + Streamlit
frontend. Generates phone numbers in bulk from a 3-part range picker
(area code / exchange range / line-number range), checks each one against
the TCPA lookup API, and lets you download the results as CSV.

## Project structure

```text
dnc_bulk_checker/
├── app.py                     # Streamlit UI — the only file you "run"
├── backend/
│   ├── config.py               # API URL, headers, timeouts — edit here if the endpoint changes
│   ├── logger_setup.py         # logging to console + logs/app.log + in-app debug panel
│   ├── number_builder.py       # builds the number list from the 3 ranges
│   ├── session_manager.py      # session token handling + connection test
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

It opens at `http://localhost:8501`.

## 2. Get a session token (do this first, every time)

The site behind the API sits behind bot-protection, which is most likely
why the old tool "sometimes just didn't work" — an automated fetch of the
homepage can silently hit a challenge page instead of a real session. To
sidestep that entirely:

1. Open the target site in a normal browser (Chrome/Edge).
2. Open DevTools (F12) → **Network** tab.
3. Do anything on the site that triggers an API call (search a number, etc.)
4. Click on a request to `api.uspeoplesearch.net/...` in the network list.
5. In the **Headers** panel, find `X-Session-ID` (or the `session` cookie
   in the Cookies panel) and copy its value.
6. In the app's sidebar, paste it into **"Paste session token / cookie / JSON"**
   and click **Apply token**.
7. Click **Test connection** — it fires one real request and tells you
   plainly whether the token works, is expired, or the site is rate-limiting.

There's also an **Auto-refresh (best effort)** button that tries to fetch a
token automatically without a browser. It works when the site happens to
serve a normal homepage, and fails cleanly (with a clear reason in the
sidebar) when it hits a bot-check page — at which point just paste a token
manually instead.

If a token expires mid-run, the engine detects the 401/403, tries one
automatic refresh, and if that fails it stops the run and tells you to
paste a fresh token in the **fatal error banner** at the top of the page —
just paste + Apply + Start again (it resumes from where it left off, see below).

## 3. Choose the number range

Phone numbers are NPA-NXX-XXXX (area code, exchange, line number). Instead
of one flat range, you pick each segment separately:

- **Area code** — the fixed 3-digit prefix, e.g. `806`.
- **Exchange range** — the next 3 digits, as a start–end range, e.g. `549`–`560`.
- **Line number range** — the last 4 digits, as a start–end range, e.g. `0000`–`9999`.

The app multiplies these out for you and shows the total count live
(`(exchange_end - exchange_start + 1) × (line_end - line_start + 1)`), so
you can see exactly how many numbers a given range produces before starting.
Start narrow (a small exchange range) to test things, then widen once
you've confirmed the token and rate work well.

## 4. Performance settings

- **Parallel threads** — more threads = faster, but a higher chance of
  getting rate-limited (HTTP 429) or the token getting flagged. Start
  around 20–40 and watch the Debug logs panel for 429s before pushing higher.
- **Chunk size** — numbers are processed in batches; between batches the
  engine sleeps for the configured seconds to ease off the API.
- **Sleep between chunks** — the pause duration mentioned above.

## 5. Run it

Click **Start**. You can **Pause**/**Resume** at any time, and **Stop** to
end the run early — either way, whatever's already been written to CSV
stays there. **Reset data** clears all three output CSVs (only enabled
while nothing is running).

Progress, live speed, ETA, and the last dozen results are shown while a run
is active. The bottom **Debug logs** panel shows the last 200 log lines
(the same ones going to `logs/app.log`) — check it first if something looks
stuck or you're getting a lot of errors.

## 6. Resume support

Every number written to any of the three output CSVs is remembered. If you
stop a run and start a new one over a range that overlaps, already-checked
numbers are automatically skipped — you won't burn time or requests
re-checking them.

## 7. Download results

Use the **Valid / DNC / Not found / All** buttons above the table to filter,
then **Download \<filter\> CSV** to export exactly that view. The raw CSVs
also live directly in `data/` if you'd rather grab them from disk.

## 8. Deploying to Streamlit Community Cloud

1. Push this `dnc_bulk_checker/` folder to a GitHub repo (a `.gitignore`
   is included so the `data/` CSVs and `logs/` don't get committed).
2. On [share.streamlit.io](https://share.streamlit.io), create a new app,
   point it at the repo, and set the main file to `app.py`.
3. No secrets are required — the session token is pasted at runtime in the
   sidebar, not stored in code.
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
2. Click **Test connection** in the sidebar — it tells you directly if the
   token is expired (401/403), you're rate-limited (429), or the response
   isn't real JSON (usually a bot-check page slipping through).
3. If you see repeated 429s, lower the thread count and/or increase the
   sleep-between-chunks setting.
4. If the token keeps expiring quickly, that's normal for this kind of
   site — just re-paste a fresh one from the browser when prompted.
