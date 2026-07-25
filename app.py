"""
app.py - Bulk DNC/TCPA Checker

Run locally with:
    streamlit run app.py

See README.md for the full usage guide (getting a session token, choosing
ranges, deploying to Streamlit Cloud).
"""

import logging
import threading
import time

import pandas as pd
import streamlit as st

from backend.logger_setup import init_logging, get_recent_logs
init_logging()
logger = logging.getLogger(__name__)

from backend.checker_engine import CheckerEngine
from backend.config import DATA_DIR, PROXIES_FILE
from backend.csv_store import CSVStore
from backend.number_builder import build_number_list, range_summary
from backend.proxy_manager import ProxyManager
from backend.session_manager import SessionManager

st.set_page_config(page_title="Bulk DNC Checker", page_icon="\U0001F4DE", layout="wide")

st.markdown(
    """
<style>
.stApp { background:#0f1117; }
section[data-testid="stSidebar"] { background:#161b27; border-right:1px solid #2a2f3e; }
div[data-testid="stMetric"] { background:#1a1f2e; border:1px solid #2a2f3e; border-radius:10px; padding:10px 16px; }
div[data-testid="stProgress"] > div > div { background:#00c49a; }
.live-feed, .log-feed {
    background:#1a1f2e; border:1px solid #2a2f3e; border-radius:10px;
    padding:12px 16px; font-family:monospace; font-size:12.5px;
    max-height:320px; overflow-y:auto; white-space:pre-wrap;
}
</style>
""",
    unsafe_allow_html=True,
)


# ════════════════════════════════════════════════════════════
#  PERSISTENT STATE
# ════════════════════════════════════════════════════════════

def _init_state():
    if "session_mgr" not in st.session_state:
        st.session_state.session_mgr = SessionManager()
    if "proxy_mgr" not in st.session_state:
        st.session_state.proxy_mgr = ProxyManager(PROXIES_FILE)
    if "csv_store" not in st.session_state:
        st.session_state.csv_store = CSVStore(DATA_DIR)
    if "engine" not in st.session_state:
        st.session_state.engine = None
    if "result_filter" not in st.session_state:
        st.session_state.result_filter = "all"


_init_state()

sm: SessionManager = st.session_state.session_mgr
pm: ProxyManager = st.session_state.proxy_mgr
store: CSVStore = st.session_state.csv_store


# ════════════════════════════════════════════════════════════
#  SIDEBAR
# ════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("## \U0001F4DE Bulk DNC Checker")
    st.markdown("---")

    st.markdown("### \U0001F522 Number Range (NPA-NXX-XXXX)")
    st.caption("Pick the 3 segments of the numbers to generate: area code, then an exchange range, then a line-number range.")

    area_code = st.text_input("Area code - first 3 digits", value="806", max_chars=3, placeholder="e.g. 713")

    st.markdown("**Exchange (next 3 digits) - range**")
    ex_col1, ex_col2 = st.columns(2)
    with ex_col1:
        exch_start = st.number_input("Exchange start", min_value=0, max_value=999, value=549, step=1)
    with ex_col2:
        exch_end = st.number_input("Exchange end", min_value=0, max_value=999, value=550, step=1)

    st.markdown("**Line number (last 4 digits) - range**")
    ln_col1, ln_col2 = st.columns(2)
    with ln_col1:
        line_start = st.number_input("Line start", min_value=0, max_value=9999, value=0, step=1)
    with ln_col2:
        line_end = st.number_input("Line end", min_value=0, max_value=9999, value=999, step=1)

    valid_area = bool(area_code) and len(area_code) == 3 and area_code.isdigit()
    valid_ranges = exch_start <= exch_end and line_start <= line_end

    info = None
    if valid_area and valid_ranges:
        info = range_summary(area_code, int(exch_start), int(exch_end), int(line_start), int(line_end))
        st.info(f"**{info['start']}** -> **{info['end']}**\n\nTotal: **{info['total']:,}** numbers")
        if info["total"] > 500_000:
            st.warning("That's a very large range. Consider narrowing it - huge ranges take a long time and use a lot of memory/browser table space.")
    else:
        if not valid_area:
            st.warning("Enter a valid 3-digit area code.")
        if not valid_ranges:
            st.warning("Each range's start must be <= its end.")

    st.markdown("---")
    st.markdown("### ⚡ Performance")
    st.caption(
        "The site only tolerates ~1 in-flight request per connection at a time "
        "(confirmed by testing — concurrent requests on the same token mostly get "
        "rejected). Without proxies, checks run one at a time regardless of thread "
        "count (~2-4/sec). Thread count only buys real speed once you've loaded "
        "multiple proxies, since each proxy gets its own token and runs independently."
    )

    max_threads = st.slider("Parallel threads", min_value=5, max_value=200, value=40, step=5,
                             help="Only helps when multiple proxies are loaded (one token per proxy, run in parallel).")
    chunk_size = st.select_slider("Chunk size", options=[500, 1000, 2500, 5000, 10000], value=2500,
                                   help="Numbers processed per batch before a short pause.")
    sleep_between = st.slider("Sleep between chunks (sec)", min_value=0.0, max_value=10.0, value=2.0, step=0.5)

    if info:
        per_connection_rate = 2.5
        effective_lanes = max(pm.healthy_count, 1) if pm.total else 1
        est_rate = min(max_threads, effective_lanes) * per_connection_rate if pm.total else per_connection_rate
        est_time = info["total"] / est_rate
        st.caption(f"Rough estimate: ~{est_rate:.1f}/sec -> ~{est_time:,.0f}s for this range")

    st.markdown("---")
    st.markdown("### \U0001F310 Proxy Pool (optional)")
    col_r, col_s = st.columns(2)
    with col_r:
        if st.button("Reload proxies", use_container_width=True):
            pm.reload()
            st.success(f"Loaded {pm.total} proxies")
    with col_s:
        if st.button("Remove dead", use_container_width=True):
            pm.remove_dead()
            st.success("Dead proxies removed")
    st.caption(f"Pool: **{pm.total}** total - **{pm.healthy_count}** healthy - **{pm.banned_count}** banned")
    if pm.total == 0:
        st.caption("No proxies loaded - requests will go out directly. That's fine for testing / smaller runs.")
    else:
        if st.button("\U0001F50C Test all proxies", use_container_width=True):
            addresses = pm.all_addresses()
            progress = st.progress(0.0)
            status_box = st.empty()
            ok_count = 0
            for i, addr in enumerate(addresses):
                proxies_dict = pm.to_requests_dict(addr)
                result = sm.test_connection(proxy=addr, proxies_dict=proxies_dict)
                if result["ok"]:
                    ok_count += 1
                    pm.mark_success(addr)
                else:
                    pm.mark_failure(addr, ban=True)
                    logger.info(f"[UI] Proxy {addr} failed test: {result['message']}")
                status_box.caption(f"Tested {i + 1}/{len(addresses)} - {addr} -> {'OK' if result['ok'] else result['message']}")
                progress.progress((i + 1) / len(addresses))
            st.success(f"{ok_count}/{len(addresses)} proxies passed. Failing ones were marked banned - click Reload proxies to bring them back if you fix them.")

    st.markdown("---")
    st.markdown("### \U0001F511 Connection")
    st.caption(
        "No manual token needed - the app fetches its own short-lived lookup token "
        "from the site automatically and refreshes it every ~14 minutes."
    )

    if st.button("\U0001F50C Test connection", use_container_width=True):
        with st.spinner("Minting a token and sending a test request..."):
            result = sm.test_connection()
        if result["ok"]:
            st.success(result["message"])
        else:
            st.error(result["message"])

    if sm.is_valid():
        st.caption(f"Token: `{sm.token_preview()}`")
    else:
        st.caption("No token minted yet - click Test connection or Start.")
    if sm.last_error:
        st.caption(f"Last error: {sm.last_error}")

    with st.expander("Advanced: manual token override"):
        st.caption(
            "Only needed if the site ever locks down its token endpoint. "
            "Paste a token/cookie captured from a real browser to force it."
        )
        token_input = st.text_area("Manual token", value="", height=70, label_visibility="collapsed")
        if st.button("Apply manual token", use_container_width=True):
            if sm.set_manual_token(token_input):
                st.success("Manual override applied.")
            else:
                st.info("Manual override cleared - back to auto-mint.")

    st.markdown("---")
    st.caption("Bulk DNC Checker v1.0")


# ════════════════════════════════════════════════════════════
#  MAIN AREA
# ════════════════════════════════════════════════════════════

st.title("\U0001F4DE Bulk DNC / TCPA Number Checker")
st.caption("Generates numbers from the ranges on the left, checks each one against the API, and buckets results into Valid / DNC / Not found.")

engine: CheckerEngine | None = st.session_state.engine
is_running = engine is not None and engine.stats.running
is_paused = engine is not None and engine.stats.paused

b1, b2, b3, b4, _ = st.columns([1, 1, 1, 1, 3])

with b1:
    start_clicked = st.button("▶ Start", type="primary", disabled=is_running, use_container_width=True)
with b2:
    pause_clicked = st.button("⏸ Pause" if not is_paused else "▶ Resume", disabled=not is_running, use_container_width=True)
with b3:
    stop_clicked = st.button("⏹ Stop", disabled=not is_running, use_container_width=True)
with b4:
    reset_clicked = st.button("\U0001F5D1 Reset data", disabled=is_running, use_container_width=True)

if start_clicked:
    if not valid_area:
        st.error("Enter a valid 3-digit area code first.")
    elif not valid_ranges:
        st.error("Fix the exchange/line ranges first (start must be <= end).")
    else:
        already_done = store.load_processed()
        numbers = build_number_list(
            area_code=area_code,
            exch_start=int(exch_start),
            exch_end=int(exch_end),
            line_start=int(line_start),
            line_end=int(line_end),
            already_done=already_done,
            shuffle=True,
        )

        if not numbers:
            st.warning("Every number in this range has already been processed. Widen the range or reset data.")
        else:
            if not sm.is_valid():
                with st.spinner("Minting a lookup token..."):
                    ok = sm.refresh()
                if not ok:
                    st.error(sm.last_error or "Could not obtain a lookup token. Click Test connection in the sidebar for details.")
                    st.stop()

            new_engine = CheckerEngine(
                session_mgr=sm,
                proxy_mgr=pm,
                csv_store=store,
                max_threads=max_threads,
                chunk_size=chunk_size,
                sleep_between_chunks=sleep_between,
            )
            st.session_state.engine = new_engine
            t = threading.Thread(target=new_engine.run, args=(numbers,), daemon=True)
            t.start()
            st.rerun()

if pause_clicked and engine:
    engine.resume() if is_paused else engine.pause()
    st.rerun()

if stop_clicked and engine:
    engine.stop()
    store.flush_all()
    st.info("Stop requested - finishing in-flight requests...")
    st.rerun()

if reset_clicked:
    store.reset()
    st.success("Output data reset.")
    st.rerun()

if engine and engine.stats.fatal_error and not engine.stats.running:
    st.error(f"Run stopped: {engine.stats.fatal_error}")

st.markdown("---")

stats = engine.stats if engine else None
counts = store.counts()


def filter_label(name, count, label):
    star = "⭐ " if st.session_state.result_filter == name else ""
    return f"{star}{label} ({count:,})"


m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
with m1:
    if st.button(filter_label("valid", counts["valid"], "✅ Valid"), use_container_width=True):
        st.session_state.result_filter = "valid"
        st.rerun()
with m2:
    if st.button(filter_label("dnc", counts["dnc"], "\U0001F6AB DNC"), use_container_width=True):
        st.session_state.result_filter = "dnc"
        st.rerun()
with m3:
    if st.button(filter_label("non_existent", counts["non_existent"], "❌ Not found"), use_container_width=True):
        st.session_state.result_filter = "non_existent"
        st.rerun()
with m4:
    total_done = counts["valid"] + counts["dnc"] + counts["non_existent"]
    if st.button(filter_label("all", total_done, "\U0001F4CA All"), use_container_width=True):
        st.session_state.result_filter = "all"
        st.rerun()
with m5:
    st.metric("⚠️ Errors", f"{stats.errors:,}" if stats else "0")
with m6:
    st.metric("Speed", f"{stats.rate_per_sec:.1f}/sec" if stats and stats.running else "-")
with m7:
    if stats and stats.running:
        eta = stats.eta_seconds
        eta_str = f"{eta:.0f}s" if eta < 60 else (f"{eta/60:.1f}m" if eta < 3600 else f"{eta/3600:.1f}h")
        st.metric("ETA", eta_str)
    else:
        st.metric("ETA", "-")

if stats and not stats.running and stats.errors:
    st.warning(
        f"{stats.errors:,} number(s) errored out this run (timeouts, rate-limits, or repeated token "
        f"rejections — see Debug logs below for the exact cause). They were NOT saved to any CSV, so "
        f"starting a new run over the same range will automatically retry them."
    )

st.markdown("---")

rows = store.read_rows(st.session_state.result_filter)
if rows:
    df = pd.DataFrame(rows)
    export_name = {
        "valid": "valid_results.csv",
        "dnc": "dnc_results.csv",
        "non_existent": "not_found_results.csv",
        "all": "all_results.csv",
    }[st.session_state.result_filter]
    st.download_button(
        label=f"⬇ Download {st.session_state.result_filter.replace('_', ' ').title()} CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=export_name,
        mime="text/csv",
        use_container_width=True,
    )
    st.dataframe(df, use_container_width=True)
else:
    st.info("No results yet for this filter.")

if stats and (stats.running or stats.processed):
    st.subheader("⚙️ Live run")
    st.progress(min(stats.progress_pct / 100, 1.0))
    st.caption(f"Chunk {stats.current_chunk}/{stats.total_chunks} - {stats.processed:,}/{stats.total_queued:,} processed")

    if stats.last_numbers:
        feed = "\n".join(
            f"{e['status']:<10} | {e['phone']} | {e.get('state','-')} | {e.get('type','-')}"
            for e in stats.last_numbers
        )
        st.markdown(f"<div class='live-feed'>{feed}</div>", unsafe_allow_html=True)
else:
    st.info("Start a run to see live activity here.")

with st.expander("\U0001F41B Debug logs (last 200 lines)"):
    log_lines = get_recent_logs(200)
    if log_lines:
        st.markdown(f"<div class='log-feed'>{chr(10).join(log_lines)}</div>", unsafe_allow_html=True)
    else:
        st.caption("No log lines yet.")

if stats and stats.running:
    time.sleep(1.5)
    st.rerun()
