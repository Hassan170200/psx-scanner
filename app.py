"""PSX Live Scanner - live prices, rule-based setups, trade plans and phone alerts."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from paper import MIN_TRADES, by_signal, stats as paper_stats, to_frame as paper_frame
from alerts import Engine, Position, market_open, send_ntfy, send_whatsapp
from broker_calls import evaluate as evaluate_call, parse_message, track_record
from signals import Costs, Settings, build_scan
import ui

PKT = ZoneInfo("Asia/Karachi")
st.set_page_config(page_title="PSX Terminal", page_icon="📈", layout="wide")
ui.inject()


@st.cache_resource
def engine() -> Engine:
    """One background engine per server: scans, records VWAP and sends alerts."""
    e = Engine(interval=60)
    e.start()
    return e


@st.cache_resource
def suggested_topic() -> str:
    return f"psx-hassan-{uuid.uuid4().hex[:10]}"


def secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:  # no secrets file
        return ""


eng = engine()
if getattr(eng, "VERSION", 0) != Engine.VERSION:  # code updated while the app was running
    eng.config = None  # silences the outdated engine's background thread
    engine.clear()
    eng = engine()

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Your settings")
    capital = st.number_input("Capital (Rs)", 1_000, 100_000_000, 50_000, 5_000)
    position_pct = st.slider("Position size (% of capital)", 10, 100, 100, 10) / 100
    stop_pct = st.slider("Max stop-loss distance (%)", 0.5, 5.0, 2.0, 0.25) / 100
    rr = st.slider("Target = risk x", 1.0, 4.0, 2.0, 0.25)
    min_rr = st.slider("Min profit vs loss after costs", 0.5, 1.5, 1.0, 0.05,
                       help="Skip setups where the profit at target (after costs and tax) is less than "
                            "this many times the loss at stop. 1.0 means a win must at least cover a loss.")
    st.divider()
    st.subheader("Filters")
    kse_only = st.toggle("KSE-100 stocks only", value=True)
    min_price = st.number_input("Min share price (Rs)", 0.0, 5_000.0, 20.0, 5.0)
    min_volume = st.number_input("Min volume today (shares)", 0, 50_000_000, 200_000, 50_000)
    lot = st.number_input("Lot size (shares)", 1, 10_000, 1, 1,
                          help="Set to your broker's minimum lot if it isn't 1.")
    st.divider()
    refresh = st.select_slider("Page refresh every", options=[30, 60, 120, 300], value=60,
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

    st.divider()
    st.subheader("Broker calls")
    calls_url = secret("BROKER_CALLS_URL")
    gh_token = secret("GITHUB_TOKEN")
    risk_budget = st.number_input("Max loss per broker call (Rs)", 200, 20_000, 1_300, 100,
                                  help="Each broker call is sized so hitting its stop loses about this much.")
    st.divider()
    st.subheader("Phone alerts")
    ntfy_topic = secret("NTFY_TOPIC")
    wa_phone, wa_key = secret("WHATSAPP_PHONE"), secret("WHATSAPP_APIKEY")
    buy_when_holding = st.toggle("Buy alerts while I hold a position", value=False,
                                 help="Off = one position at a time: no new buy alerts until you sell.")
    st.markdown("**ntfy push** (main)")
    if ntfy_topic:
        st.success("Connected. Alerts run in the background, even with this page closed.")
        if st.button("Send test push"):
            ok, msg = send_ntfy(ntfy_topic, "✅ <b>PSX Scanner test alert</b>\nPush notifications are working.")
            (st.toast if ok else st.error)("Test sent. Check the ntfy app." if ok else msg)
    else:
        st.info("Not set up yet. See README → ntfy alerts. Your private channel name:")
        st.code(suggested_topic(), language=None)
    st.markdown("**WhatsApp** (optional)")
    if wa_phone and wa_key:
        st.success("Connected.")
        if st.button("Send test WhatsApp"):
            ok, msg = send_whatsapp(wa_phone, wa_key, "✅ <b>PSX Scanner</b> test alert. WhatsApp notifications are working.")
            (st.toast if ok else st.error)("Test sent. Check WhatsApp." if ok else msg)
    else:
        st.caption("Not set up. Optional, see README → WhatsApp alerts.")
    st.caption(f"Background scanner: {'running' if eng.running else 'stopped'}"
               + (f" · last scan {eng.last_tick}" if eng.last_tick else ""))

cfg = Settings(capital=capital, position_pct=position_pct, stop_pct=stop_pct, reward_risk=rr, min_rr=min_rr,
               min_price=min_price, min_volume=min_volume, lot_size=int(lot))
costs = Costs(commission_rate=comm, min_comm_per_share=comm_min, sales_tax=stax, cgt=cgt)
# The latest settings drive the background alerts
eng.config = {"settings": cfg, "costs": costs, "kse_only": kse_only, "open_t": open_t,
              "close_t": close_t, "ntfy_topic": ntfy_topic, "calls_url": calls_url,
              "wa_phone": wa_phone, "wa_key": wa_key, "buy_when_holding": buy_when_holding,
              "gh_token": gh_token}

def get_board() -> pd.DataFrame | None:
    if eng.board is None or time.time() - eng.board_at > 150:
        eng.fetch_board()
    if not eng.avg_vol:
        eng.refresh_reference()
    return eng.board


def broker_section(prices: dict) -> None:
    eng.refresh_calls(calls_url)
    ui.panel("CALLS", "Broker calls", f"Calls file updated {eng.calls_updated or 'never'}")
    if not calls_url:
        st.info("Broker calls aren't connected yet. Add BROKER_CALLS_URL to the app's Secrets (see README).")
    c1, c2 = st.columns([1, 4])
    if c1.button("Update now", icon="🔄", help="Reload the latest calls and recheck prices and history now."):
        eng.refresh_calls(calls_url, force=True)
        for c in eng.all_calls():
            eng.history(c["symbol"], force=True)
        st.toast("Broker calls updated.")
    c2.caption("Claude reads the WhatsApp channels hourly on market days (9:40 am–3:40 pm). "
               "Update now reloads them; you can also paste a call below.")
    if eng.calls_error:
        st.warning(f"Couldn't load the calls file: {eng.calls_error}")
    with st.expander("Paste a call from WhatsApp"):
        txt = st.text_area("Broker's message", height=140, key="paste_call")
        src = st.selectbox("Source", ["Nadeem Chawla-PSX", "KTrade", "Other"], key="paste_src")
        if st.button("Add call"):
            call = parse_message(txt, source=src)
            if call:
                eng.add_pasted(call)
                st.success(f"Added {call['symbol']}: buy {call['entry_low']}–{call['entry_high']}, "
                           f"stop {call['stop']}, targets {call['targets']}")
            else:
                st.error("Couldn't find a symbol with a stop-loss or target in that message.")

    calls = sorted(eng.all_calls(), key=lambda c: c.get("date", ""), reverse=True)[:15]
    if not calls:
        ui.empty("No calls yet. New calls from Monday 16 Nov will appear here.")
        return
    evals = []
    for c in calls:
        ev = evaluate_call(c, prices.get(c["symbol"]), eng.history(c["symbol"]), costs, risk_budget)
        evals.append(ev)
        ui.call_card(c, ev)
        if c in eng.pasted and st.button("Remove", key=f"rm-{c['id']}"):
            eng.remove_pasted(c["id"])
            st.rerun()
    tr = track_record(evals)
    if tr["closed"]:
        st.caption(f"Track record of these calls: {tr['closed']} finished: {tr['wins']} hit a target, "
                   f"{tr['partial']} hit a target then stopped, {tr['losses']} stopped out.")


# ---------------------------------------------------------------- live section
@st.fragment(run_every=refresh)
def live() -> None:
    now = datetime.now(PKT)
    is_open = market_open(now, open_t, close_t)
    board = get_board()
    if board is None:
        ui.status_bar(is_open, now.strftime("%I:%M:%S %p"), 0, 0, 0, 0, "unavailable")
        st.error(
            "Couldn't reach the PSX data portal from this server.\n\n"
            f"Details: {eng.board_error}\n\n"
            "If this keeps happening on Streamlit Cloud, PSX may be blocking foreign servers. "
            "Run the same app on your laptop instead (see README)."
        )
        return

    vwaps = eng.vwaps()
    scan = build_scan(board, eng.avg_vol, eng.kse100, vwaps,
                      eng.session_fraction(now, open_t, close_t), cfg, costs, kse_only)
    prices = dict(zip(board["symbol"], board["current"]))

    # toasts for alerts the background engine sent since this browser last looked
    seen = st.session_state.setdefault("seen_alerts", {a.key + a.at for a in eng.log})
    for a in eng.log:
        if a.key + a.at not in seen:
            st.toast(a.text.split("\n")[0].replace("<b>", "").replace("</b>", ""), icon="🔔")
            seen.add(a.key + a.at)

    moves = board["current"] - board["ldcp"] if "ldcp" in board.columns else board.get("change", pd.Series(dtype=float))
    upd = datetime.fromtimestamp(eng.board_at, PKT).strftime("%a %d %b, %I:%M %p")
    ui.status_bar(is_open, now.strftime("%a %I:%M %p"), int((moves > 0).sum()), int((moves < 0).sum()),
                  int(scan["actionable"].sum()) if not scan.empty else 0, len(vwaps), upd)
    ui.ticker_tape(board)
    if not is_open:
        st.caption("Market is closed: these are the last available prices. Setups and alerts work during "
                   "trading hours.")

    # ---- my position
    ui.panel("POS", "My open position", "Watched for stop, target, breakeven and the close")
    if eng.positions:
        for p in list(eng.positions):
            px = prices.get(p.symbol)
            if px is None or pd.isna(px):
                ui.empty(f"{p.symbol}: no live price right now.")
                continue
            pl = (px - p.entry) * p.shares - costs.per_side(p.shares, p.entry) - costs.per_side(p.shares, px)
            ui.position_card(p, px, pl)
            if st.button(f"I sold {p.symbol}", key=f"sold-{p.symbol}"):
                eng.close_position(p.symbol)
                st.rerun()
    else:
        ui.empty("No open position. When you buy, press I bought this on a setup (or add it below) "
                 "so the app watches your stop and target and alerts you when to sell.")
    with st.expander("Add a position manually"):
        with st.form("add_pos", clear_on_submit=True):
            f1, f2, f3 = st.columns(3)
            sym = f1.text_input("Symbol").strip().upper()
            entry = f2.number_input("Buy price", 0.0, 100_000.0, 0.0, 0.01)
            shares = f3.number_input("Shares", 0, 10_000_000, 0, 1)
            f4, f5 = st.columns(2)
            stop = f4.number_input("Stop-loss (0 = auto)", 0.0, 100_000.0, 0.0, 0.01)
            target = f5.number_input("Target (0 = auto)", 0.0, 100_000.0, 0.0, 0.01)
            if st.form_submit_button("Watch this position") and sym and entry > 0 and shares > 0:
                stop = stop or round(entry * (1 - stop_pct), 2)
                target = target or round(entry + rr * (entry - stop), 2)
                eng.add_position(Position(sym, entry, int(shares), stop, target,
                                          opened=now.strftime("%Y-%m-%d %H:%M")))
                st.rerun()

    # ---- top setups
    top = scan[scan["actionable"]].head(5) if not scan.empty else scan
    ui.panel("SETUPS", "Top setups now", f"Sized for Rs {capital:,.0f} capital at {position_pct:.0%}")
    if scan.empty:
        ui.empty("No stocks pass your filters. Try lowering min volume or turning off KSE-100 only.")
    elif top.empty:
        ui.empty("No buy setups at the moment. Patience is a position.")
    for r in top.itertuples():
        ui.setup_card(r)
        if st.button("I bought this", key=f"buy-{r.symbol}", icon="✅",
                     help="Watch this trade: alerts at stop, target, breakeven and before the close."):
            eng.add_position(Position(r.symbol, float(r.entry), int(r.shares), float(r.stop),
                                      float(r.target), opened=now.strftime("%Y-%m-%d %H:%M")))
            st.rerun()

    broker_section(prices)

    if scan.empty:
        return

    # ---- full table
    ui.panel("SCAN", "Scanner", f"{len(scan)} stocks pass your filters")
    show = scan[["symbol", "signal", "score", "current", "change_pct", "rvol", "volume",
                 "pos_in_range", "vwap", "entry", "stop", "target", "shares",
                 "net_at_target", "loss_at_stop"]].rename(columns={
        "symbol": "Symbol", "signal": "Signal", "score": "Score", "current": "Price",
        "change_pct": "Chg %", "rvol": "Vol vs normal", "volume": "Volume",
        "pos_in_range": "Pos in day range", "vwap": "VWAP", "entry": "Buy", "stop": "Stop",
        "target": "Target", "shares": "Shares", "net_at_target": "Net @ target (Rs)",
        "loss_at_stop": "Loss @ stop (Rs)"})
    st.dataframe(ui.style_scan(show), hide_index=True, width="stretch", height=min(440, 38 + 35 * len(show)),
                 column_config={"Pos in day range": st.column_config.ProgressColumn(
                     min_value=0, max_value=1, format="%.2f")})

    # ---- alerts log
    if eng.log:
        ui.panel("LOG", "Today's alerts", f"{len(eng.log)} sent")
        for a in reversed(eng.log[-10:]):
            st.markdown(f"`{a.at}` " + a.text.replace("\n", "  \n"), unsafe_allow_html=True)

    # ---- chart for one stock
    ui.panel("CHART", "Intraday chart", "Price and VWAP from today's snapshots")
    syms = scan["symbol"].tolist()
    held = [p.symbol for p in eng.positions if p.symbol in syms]
    sym = st.selectbox("Stock", syms, index=syms.index(held[0]) if held else 0, key="chart_sym")
    with eng.lock:
        snaps = list(eng.snaps.get(sym, []))
    if len(snaps) >= 3:
        ch = pd.DataFrame(snaps, columns=["t", "Price", "cumvol"])
        ch["t"] = pd.to_datetime(ch["t"], unit="s", utc=True).dt.tz_convert(PKT)
        dv = ch["cumvol"].diff().clip(lower=0)
        mid = (ch["Price"] + ch["Price"].shift()) / 2
        ch["VWAP"] = (mid * dv).cumsum() / dv.cumsum()
        st.line_chart(ch.set_index("t")[["Price", "VWAP"]], color=["#FFB020", "#5CC8E8"])
    else:
        ui.empty("Not enough snapshots yet for this stock. The chart fills in during market hours.")

live()


def paper_section() -> None:
    eng.paper.connect(gh_token)
    trades = eng.paper.snapshot()
    meta = f"Every buy setup since {trades[0].date}" if trades else "Every buy setup, followed by your plan's rules"
    ui.panel("PAPER", "Paper trades", meta)
    if not trades:
        ui.empty("Recording starts at the next market open. Every buy setup is logged at its buy price with the "
                 "suggested share count, then sold at the target or stop, with the stop moved to breakeven at "
                 "+1× risk, and sold 15 minutes before the close if neither is hit.")
    else:
        ui.paper_summary(paper_stats(trades), MIN_TRADES)
        df = paper_frame(trades).iloc[::-1].reset_index(drop=True)
        st.dataframe(ui.style_paper(df), hide_index=True, width="stretch", height=min(420, 38 + 35 * len(df)))
        c1, c2 = st.columns([1, 3])
        c1.download_button("Download CSV", df.to_csv(index=False), "paper_trades.csv", "text/csv",
                           icon="⬇️")
        with c2.expander("Results by signal type"):
            st.dataframe(by_signal(trades), hide_index=True, width="stretch")
    if eng.paper.gist_id:
        st.caption("Backed up to a private gist in your GitHub account, so restarts don't lose it. "
                   "A summary is pushed to your phone 10 minutes before the close.")
    elif eng.paper.remote_error:
        st.caption(f"{eng.paper.remote_error}. The log is kept on the app server meanwhile, and a restart wipes it.")
    else:
        st.caption("Kept on the app server only, so a restart wipes it. Add GITHUB_TOKEN to Secrets to back it up "
                   "(see README → Paper trades). A summary is pushed to your phone 10 minutes before the close.")


paper_section()

with st.expander("How the signals and alerts work"):
    st.markdown(
        f"""
- **Momentum breakout**: up 1–7% today, volume at least 1.5× its normal pace for this time of day,
  price in the top quarter of today's range and above VWAP.
- **VWAP bounce**: green on the day, volume at or above normal pace, price within 0.6% above VWAP.
- **Volume building**: volume 2× normal but price flat. Watch it; don't buy yet.
- **Overextended** (up 7%+) and **Falling** (down 3%+) are flagged so you avoid chasing or catching knives.
- **Stop-loss**: the tightest of VWAP, today's low, or {stop_pct:.1%} below entry, keeping at least 0.5% room,
  and wide enough that the profit at target is at least {min_rr:.2f}× the loss at stop after costs.
  If no stop within {stop_pct:.1%} manages that, the setup shows as **Costs too high - skip**. **Target**: {rr}× the risk.
- **VWAP** counts only after the app has watched a stock for 45 minutes. Before that (early morning, or after the
  app wakes up mid-day) there are no VWAP bounce signals, because a fresh VWAP sits right on the price.
- **Costs**: commission max(Rs {comm_min:.2f}/share, {comm:.2%}) + {stax:.0%} sales tax, exchange/CDC fees,
  and {cgt:.0%} capital gains tax on profit.
- **Alerts** (ntfy push and/or WhatsApp, checked every minute in market hours): new buy setups (max 3 per scan, once per stock
  per day, paused while you hold a position unless you switch that on); for your position: stop hit → sell,
  target hit → sell, up 1× risk → move stop to breakeven, and 15 minutes before the close.
- **Paper trades**: every buy setup (once per stock per day, until 30 min before the close) is logged at its buy
  price and followed with the same rules: target, stop, breakeven at +1× risk, sold 15 min before the close.
  Net results include costs and tax. Use it to see whether the signals make money before trading them.
- The background scanner runs while the app is awake. Open the site once each morning to wake it.
"""
    )
st.caption("Not financial advice. Data comes from the public PSX Data Portal and may be delayed or "
           "unavailable. Always confirm the price in your broker's app before placing an order.")
