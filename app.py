"""PSX Live Scanner - live prices, rule-based setups and full-capital trade plans."""
from __future__ import annotations

import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from psx_client import PSXClient, PSXError
from signals import ACTIONABLE, Costs, Settings, build_scan, session_vwap

PKT = ZoneInfo("Asia/Karachi")
st.set_page_config(page_title="PSX Live Scanner", page_icon="📈", layout="wide")


# ---------------------------------------------------------------- data layer
@st.cache_resource
def client() -> PSXClient:
    return PSXClient()


@st.cache_resource
def snapshot_store() -> dict:
    """Intraday price/volume snapshots, shared by every visitor of this app."""
    return {"day": None, "snaps": {}, "lock": threading.Lock()}


@st.cache_data(ttl=50, show_spinner=False)
def load_board() -> pd.DataFrame:
    return client().market_watch()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_avg_volume() -> dict:
    try:
        sc = client().screener()
        if {"symbol", "volume_avg_30d"} <= set(sc.columns):
            return dict(zip(sc["symbol"], sc["volume_avg_30d"]))
    except PSXError:
        pass
    return {}


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_kse100() -> set:
    try:
        return set(client().index_members("KSE100"))
    except PSXError:
        return set()


def record_snapshots(board: pd.DataFrame) -> None:
    store = snapshot_store()
    today = datetime.now(PKT).date()
    now = time.time()
    with store["lock"]:
        if store["day"] != today:
            store["day"], store["snaps"] = today, {}
        for sym, px, vol in zip(board["symbol"], board["current"], board.get("volume", pd.Series(0, index=board.index))):
            if pd.isna(px) or pd.isna(vol):
                continue
            series = store["snaps"].setdefault(sym, [])
            if not series or vol != series[-1][2]:
                series.append((now, float(px), float(vol)))


def current_vwaps() -> dict:
    store = snapshot_store()
    with store["lock"]:
        return {s: v for s, snaps in store["snaps"].items() if (v := session_vwap(snaps))}


def session_fraction(open_t: dtime, close_t: dtime) -> tuple[float, bool]:
    now = datetime.now(PKT)
    if now.weekday() >= 5:
        return 1.0, False
    start = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
    end = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
    if now < start:
        return 1.0, False
    if now > end:
        return 1.0, False
    return (now - start) / (end - start), True


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Your settings")
    capital = st.number_input("Capital (Rs)", 1_000, 100_000_000, 50_000, 5_000)
    position_pct = st.slider("Position size (% of capital)", 10, 100, 100, 10) / 100
    stop_pct = st.slider("Max stop-loss distance (%)", 0.5, 5.0, 2.0, 0.25) / 100
    rr = st.slider("Target = risk x", 1.0, 4.0, 2.0, 0.25)
    st.divider()
    st.subheader("Filters")
    kse_only = st.toggle("KSE-100 stocks only", value=True)
    min_price = st.number_input("Min share price (Rs)", 0.0, 5_000.0, 20.0, 5.0)
    min_volume = st.number_input("Min volume today (shares)", 0, 50_000_000, 200_000, 50_000)
    lot = st.number_input("Lot size (shares)", 1, 10_000, 1, 1,
                          help="Set to your broker's minimum lot if it isn't 1.")
    st.divider()
    refresh = st.select_slider("Auto-refresh every", options=[30, 60, 120, 300], value=60,
                               format_func=lambda s: f"{s} sec")
    with st.expander("Market hours (PKT)"):
        open_t = st.time_input("Opens", dtime(9, 30))
        close_t = st.time_input("Closes", dtime(15, 30))
        st.caption("Adjust for Fridays or Ramadan timings.")
    with st.expander("Broker costs & tax"):
        st.caption("Defaults = KTrade standard plan: 0.25% for shares Rs 20+, Rs 0.05/share below. "
                   "Premier-I plan: set 0.20%.")
        comm = st.number_input("Commission rate (%)", 0.0, 2.5, 0.25, 0.01) / 100
        comm_min = st.number_input("Min commission per share (Rs)", 0.0, 1.0, 0.05, 0.01)
        stax = st.number_input("Sales tax on commission (%)", 0.0, 20.0, 16.0, 1.0) / 100
        cgt = st.number_input("Capital gains tax (%)", 0.0, 30.0, 15.0, 1.0) / 100

cfg = Settings(capital=capital, position_pct=position_pct, stop_pct=stop_pct, reward_risk=rr,
               min_price=min_price, min_volume=min_volume, lot_size=int(lot))
costs = Costs(commission_rate=comm, min_comm_per_share=comm_min, sales_tax=stax, cgt=cgt)

st.title("📈 PSX Live Scanner")
st.caption("Live PSX prices → rule-based setups → full trade plan with costs and tax. "
           "Signals are screens, not predictions. You decide every trade.")


# ---------------------------------------------------------------- live section
@st.fragment(run_every=refresh)
def live() -> None:
    frac, is_open = session_fraction(open_t, close_t)
    try:
        board = load_board()
    except PSXError as e:
        st.error(
            "Couldn't reach the PSX data portal from this server.\n\n"
            f"Details: {e}\n\n"
            "If this keeps happening on Streamlit Cloud, PSX may be blocking foreign servers. "
            "Run the same app on your laptop instead (see README)."
        )
        return

    if is_open:
        record_snapshots(board)
    vwaps = current_vwaps()
    scan = build_scan(board, load_avg_volume(), load_kse100(), vwaps, frac, cfg, costs, kse_only)

    now = datetime.now(PKT).strftime("%a %d %b, %I:%M:%S %p")
    if "ldcp" in board.columns:
        moves = board["current"] - board["ldcp"]
    else:
        moves = board.get("change", pd.Series(dtype=float))
    up, down = int((moves > 0).sum()), int((moves < 0).sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Market", "OPEN" if is_open else "CLOSED")
    c2.metric("Stocks up / down", f"{up} / {down}")
    c3.metric("Setups right now", int(scan["actionable"].sum()) if not scan.empty else 0)
    c4.metric("VWAP tracking", f"{len(vwaps)} stocks")
    st.caption(f"Updated {now} PKT · source: dps.psx.com.pk{board.attrs.get('source', '')} · "
               f"refreshes every {refresh}s")
    if not is_open:
        st.info("Market looks closed, so these are the last available prices. "
                "Setups are most useful during trading hours.")
    if is_open and len(vwaps) < 10:
        st.caption("VWAP builds up while the app stays open. Give it ~10 minutes after the open.")

    if scan.empty:
        st.warning("No stocks pass your filters. Try lowering min volume or turning off KSE-100 only.")
        return

    # ---- top setups
    st.subheader("Top setups now")
    top = scan[scan["actionable"]].head(5)
    if top.empty:
        st.write("No buy setups at the moment. Patience is a position.")
    for r in top.itertuples():
        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            h1.markdown(f"### {r.symbol}  ·  {r.signal}")
            h2.markdown(f"**Rs {r.current:,.2f}**  ({r.change_pct:+.2f}% today) · "
                        f"volume {r.rvol:.1f}× normal")
            m = st.columns(3)
            m[0].metric("Buy at", f"{r.entry:,.2f}")
            m[1].metric("Stop-loss", f"{r.stop:,.2f}")
            m[2].metric("Target", f"{r.target:,.2f}")
            n = st.columns(3)
            n[0].metric("Shares", f"{int(r.shares):,}", f"Rs {r.position_rs:,.0f}", delta_color="off",
                        delta_arrow="off")
            n[1].metric("Net at target (Rs)", f"{r.net_at_target:+,.0f}", "after costs & tax", delta_color="off",
                        delta_arrow="off")
            n[2].metric("Loss at stop (Rs)", f"{r.loss_at_stop:,.0f}", f"{r.loss_at_stop / capital:.1%} of capital",
                        delta_color="red", delta_arrow="off")
            if r.rr_after_costs is not None and r.rr_after_costs < 1.0:
                st.caption("⚠️ After costs and tax, the profit at target is smaller than the loss at stop. Skip or widen the target.")

    # ---- full table
    st.subheader("Scanner")
    show = scan[["symbol", "signal", "score", "current", "change_pct", "rvol", "volume",
                 "pos_in_range", "vwap", "entry", "stop", "target", "shares",
                 "net_at_target", "loss_at_stop"]].rename(columns={
        "symbol": "Symbol", "signal": "Signal", "score": "Score", "current": "Price",
        "change_pct": "Chg %", "rvol": "Vol vs normal", "volume": "Volume",
        "pos_in_range": "Pos in day range", "vwap": "VWAP", "entry": "Buy", "stop": "Stop",
        "target": "Target", "shares": "Shares", "net_at_target": "Net @ target (Rs)",
        "loss_at_stop": "Loss @ stop (Rs)"})
    st.dataframe(show, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(show)),
                 column_config={
                     "Chg %": st.column_config.NumberColumn(format="%.2f"),
                     "Vol vs normal": st.column_config.NumberColumn(format="%.1f×"),
                     "Volume": st.column_config.NumberColumn(format="localized"),
                     "Net @ target (Rs)": st.column_config.NumberColumn(format="localized"),
                     "Loss @ stop (Rs)": st.column_config.NumberColumn(format="localized"),
                     "Pos in day range": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
                     "VWAP": st.column_config.NumberColumn(format="%.2f"),
                 })

    # ---- chart for one stock
    st.subheader("Intraday chart")
    sym = st.selectbox("Stock", scan["symbol"].tolist(), index=0, key="chart_sym")
    store = snapshot_store()
    with store["lock"]:
        snaps = list(store["snaps"].get(sym, []))
    if len(snaps) >= 3:
        ch = pd.DataFrame(snaps, columns=["t", "Price", "cumvol"])
        ch["t"] = pd.to_datetime(ch["t"], unit="s", utc=True).dt.tz_convert(PKT)
        dv = ch["cumvol"].diff().clip(lower=0)
        mid = (ch["Price"] + ch["Price"].shift()) / 2
        ch["VWAP"] = (mid * dv).cumsum() / dv.cumsum()
        st.line_chart(ch.set_index("t")[["Price", "VWAP"]])
    else:
        st.caption("Not enough snapshots yet for this stock. The chart fills in while the app is open.")


live()

with st.expander("How the signals work"):
    st.markdown(
        f"""
- **Momentum breakout**: up 1–7% today, volume at least 1.5× its normal pace for this time of day,
  price in the top quarter of today's range and above VWAP.
- **VWAP bounce**: green on the day, volume at or above normal pace, price within 0.6% above VWAP.
- **Volume building**: volume 2× normal but price flat. Watch it; don't buy yet.
- **Overextended** (up 7%+) and **Falling** (down 3%+) are flagged so you avoid chasing or catching knives.
- **Stop-loss**: the tightest of VWAP, today's low, or {stop_pct:.1%} below entry, keeping at least 0.5% room.
  **Target**: {rr}× the risk.
- **Costs**: commission max(Rs {comm_min:.2f}/share, {comm:.2%}) + {stax:.0%} sales tax, exchange/CDC fees,
  and {cgt:.0%} capital gains tax on profit.
- "Normal" volume = the stock's 30-day average from the PSX screener, scaled to how much of the session has passed.
"""
    )
st.caption("Not financial advice. Data comes from the public PSX Data Portal and may be delayed or "
           "unavailable. Always confirm the price in your broker's app before placing an order.")
