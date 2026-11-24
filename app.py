"""PSX Live Scanner - live prices, rule-based setups, trade plans and phone alerts."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from paper import GROUPS, MIN_TRADES, breakdown, journal_frame, stats as paper_stats, taken, to_frame as paper_frame
from alerts import Engine, Position, market_open, position_pl, send_ntfy, send_whatsapp
from broker_calls import evaluate as evaluate_call, parse_message, track_record
from signals import Costs, Settings, build_scan
import swing
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
    st.header("Your money")
    total_capital = st.number_input("Total capital (Rs)", 1_000, 100_000_000, 50_000, 5_000)
    core_pct = st.slider("Core: hold the market (%)", 0, 100, 80, 5,
                         help="Backtest 2022–2026: simply holding the KSE-100 beat every trading rule tested. "
                              "The core is bought and held; only the rest (satellite) is traded.")
    core_amt = total_capital * core_pct / 100
    capital = total_capital - core_amt
    st.caption(f"Core **Rs {core_amt:,.0f}** held · Satellite **Rs {capital:,.0f}** for trades")
    position_pct = st.slider("Satellite used per trade (%)", 10, 100, 100, 10) / 100
    st.divider()
    st.subheader("Swing rules (satellite)")
    sw_top = st.select_slider("Only the strongest stocks (by 3-month return)", options=[5, 10, 20, 30], value=20)
    sw_hold = st.slider("Sell after (trading days)", 2, 15, 4)
    sw_rr = st.slider("Swing target = risk x", 1.5, 4.0, 2.0, 0.5)
    sw_max_stop = st.slider("Max swing stop (%)", 2.0, 8.0, 5.0, 0.5) / 100
    with st.expander("Intraday rules (paper only)"):
        stop_pct = st.slider("Max stop-loss distance (%)", 0.5, 5.0, 2.0, 0.25) / 100
        rr = st.slider("Target = risk x", 1.0, 4.0, 2.0, 0.25)
        min_rr = st.slider("Min profit vs loss after costs", 0.5, 1.5, 1.0, 0.05,
                           help="Skip setups where the profit at target (after costs and tax) is less than "
                                "this many times the loss at stop. 1.0 means a win must at least cover a loss.")
        mom_window = st.slider("Momentum entries only in the first (minutes)", 15, 330, 45, 15,
                               help="Paper trades 17–23 Nov: momentum bought after 10:15 lost money.")
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
    swing_alerts = st.toggle("Swing buy alerts (3:00–3:27 pm)", value=True)
    intraday_alerts = st.toggle("Intraday buy alerts", value=False,
                                help="Off by default: intraday signals lost money on paper (17–23 Nov). "
                                     "They're still paper-traded so you can watch them improve.")
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
               min_price=min_price, min_volume=min_volume, lot_size=int(lot), momentum_window_min=mom_window)
costs = Costs(commission_rate=comm, min_comm_per_share=comm_min, sales_tax=stax, cgt=cgt)
sw_cfg = swing.SwingSettings(capital=capital * position_pct, top_n=sw_top, hold_days=sw_hold, reward_risk=sw_rr,
                             max_stop=sw_max_stop)
# The latest settings drive the background alerts
eng.config = {"settings": cfg, "costs": costs, "kse_only": kse_only, "open_t": open_t,
              "close_t": close_t, "ntfy_topic": ntfy_topic, "calls_url": calls_url,
              "wa_phone": wa_phone, "wa_key": wa_key, "buy_when_holding": buy_when_holding,
              "gh_token": gh_token, "swing": sw_cfg, "swing_alerts": swing_alerts,
              "intraday_alerts": intraday_alerts}
eng.ensure_history()

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


# ---------------------------------------------------------------- strategy sections
ETFS = ["UBLPETF", "NITGETF", "MZNPETF", "NBPGETF", "JSMFETF", "MIIETF"]


def fmt_table(df: pd.DataFrame, pct_cols=(), rs_cols=(), dec_cols=()) -> pd.DataFrame:
    """Numbers as readable text (dash for blanks): percentages, signed rupees, decimals."""
    out = df.copy()

    def f(fn):
        return lambda v: "–" if v is None or (isinstance(v, float) and pd.isna(v)) else fn(v)
    for c in pct_cols:
        if c in out.columns:
            out[c] = out[c].map(f(lambda v: f"{v:.0%}" if c == "Winners" else f"{v:+.1%}"))
    for c in rs_cols:
        if c in out.columns:
            out[c] = out[c].map(f(lambda v: f"{v:+,.0f}"))
    for c in dec_cols:
        if c in out.columns:
            out[c] = out[c].map(f(lambda v: f"{v:.1f}"))
    return out


def core_section(prices: dict) -> None:
    ui.panel("CORE", "Core: hold the market",
             f"Rs {core_amt:,.0f} of Rs {total_capital:,.0f} · buy once, hold, add monthly")
    status = swing.index_status(eng.index_hist) if eng.index_hist is not None else {}
    if status:
        trend = ('<span class="up">Uptrend</span>' if status["uptrend"] else '<span class="dn">No uptrend</span>')
        ui.card(ui.cells([
            ("KSE-100", f"{status['level']:,.0f}", f"close {status['date']:%d %b}"),
            ("1 month", ui.signed(status["ret_1m"] * 100, 1, "%") if status["ret_1m"] is not None else "–", ""),
            ("3 months", ui.signed(status["ret_3m"] * 100, 1, "%") if status["ret_3m"] is not None else "–", ""),
            ("1 year", ui.signed(status["ret_1y"] * 100, 1, "%") if status["ret_1y"] is not None else "–", ""),
            ("From 1-year high", ui.signed(status["from_high"] * 100, 1, "%"), ""),
            ("Trend", trend, "close > 20-day > 50-day avg"),
        ]))
    else:
        ui.empty("Loading KSE-100 history…")
    st.markdown(
        "**Why a core:** on real PSX prices from 2022 to today, simply holding the KSE-100 beat every trading "
        "rule we tested, in nearly every year. **How:** buy the basket below (or a KSE-100 style ETF) with your "
        "core money, keep it through the ups and downs, add new savings each month, and rebalance every 3 months. "
        "Expect drops: the index fell 23% at one point in the last year.")
    if core_amt <= 0:
        return
    basket = swing.core_basket(eng.index_weights, prices, core_amt, n=8)
    if basket.empty:
        ui.empty("Index weights not loaded yet.")
    else:
        show = pd.DataFrame({
            "Symbol": basket["symbol"], "Index weight": basket["weight"].map(lambda v: f"{v:.2f}%"),
            "Your split": basket["share"].map(lambda v: f"{v:.0%}"), "Price": basket["price"].map(lambda v: f"{v:,.2f}"),
            "Shares": basket["shares"].map(lambda v: f"{v:,}"), "Cost (Rs)": basket["cost"].map(lambda v: f"{v:,.0f}")})
        st.dataframe(show, hide_index=True, width="stretch")
        st.caption(f"The 8 biggest KSE-100 companies, split by their index weight: Rs {basket['cost'].sum():,.0f} "
                   "invested. Buying costs ≈ 0.3% once; you pay no commission again until you sell. "
                   "Bigger core = more stocks: change the split in the sidebar.")
    etf = eng.board[eng.board["symbol"].isin(ETFS)] if eng.board is not None else pd.DataFrame()
    with st.expander("Or use an ETF (one purchase holds a whole basket)"):
        st.markdown("ETFs listed on PSX that hold a basket of big stocks. Check each fund's index and fees on its "
                    "PSX page first; many trade only a few thousand units a day, so use a limit order.")
        if not etf.empty:
            cols = [c for c in ("symbol", "current", "change_pct", "volume") if c in etf.columns]
            st.dataframe(etf[cols].rename(columns={"symbol": "ETF", "current": "Price", "change_pct": "Chg %",
                                                   "volume": "Volume today"}), hide_index=True, width="stretch")
        else:
            st.caption(", ".join(ETFS))


def swing_section(board: pd.DataFrame, now: datetime, is_open: bool) -> None:
    ui.panel("SWING", "Swing setups (satellite)",
             f"Rs {sw_cfg.capital:,.0f} per trade · buy 3:00–3:27 pm · hold up to {sw_hold} days")
    if not eng.history_ready():
        done, tot = eng.hist_progress
        ui.empty(f"Loading daily price history for the swing rules ({done}/{tot or '…'} stocks). "
                 "This takes a couple of minutes once a day; refresh shortly.")
        return
    status = swing.index_status(eng.index_hist)
    if not status.get("uptrend"):
        st.warning("Market filter is off: the KSE-100 isn't in an uptrend (close above its 20-day average and the "
                   "20-day above the 50-day). No swing buys until it is. In the backtest, trading without this "
                   "filter lost money.")
    key = (now.strftime("%Y-%m-%d %H:%M")[:-1], tuple(sorted(sw_cfg.__dict__.items())))
    cache = st.session_state.get("swing_live")
    if not cache or cache[0] != key:
        cand, ok = swing.live_candidates(eng.hist, board, eng.index_hist, None, now.date(), sw_cfg, costs)
        st.session_state["swing_live"] = (key, cand, ok)
    _, cand, ok = st.session_state["swing_live"]
    if cand is None or cand.empty:
        ui.empty("No swing setups right now. They're checked against today's prices; the decision is made in the "
                 "last 30 minutes before the close.")
        return
    if not ok:
        with st.expander(f"Watch list: {len(cand)} stocks would qualify if the market were in an uptrend"):
            for r in cand.head(3).itertuples():
                ui.swing_card(r)
        return
    if is_open and now.time() < (datetime.combine(now.date(), close_t) - pd.Timedelta(minutes=35)).time():
        st.caption("Early look: these would qualify if the day closed now. Wait for 3:00 pm before buying.")
    for r in cand.head(3).itertuples():
        ui.swing_card(r)
        if st.button("I bought this (swing)", key=f"swbuy-{r.symbol}", icon="✅",
                     help="Watch it for stop, target, breakeven and the day-4 time stop."):
            eng.add_position(Position(r.symbol, float(r.entry), int(r.shares), float(r.stop), float(r.target),
                                      opened=now.strftime("%Y-%m-%d %H:%M"), kind="swing"))
            st.rerun()


def backtest_section() -> None:
    ui.panel("BACKTEST", "Backtest: swing rules vs holding the market",
             "Real PSX daily prices · KTrade costs · 15% tax on gains")
    if not eng.history_ready():
        ui.empty("Waiting for the price history to load.")
        return
    with st.spinner("Running the backtest on 3+ years of prices…"):
        trades = eng.backtest_trades(costs)
    last = eng.index_hist["date"].max()
    yr = pd.DateOffset(years=1)
    periods = [("Last 12 months", last - yr, None), ("12–24 months ago", last - 2 * yr, last - yr),
               ("24–36 months ago", last - 3 * yr, last - 2 * yr), ("Last 3 years", last - 3 * yr, None)]
    tab = swing.period_table(trades, eng.index_hist, periods, sw_cfg.capital)
    beat = int((tab["Swing return"].iloc[:3] > tab["KSE-100 buy and hold"].iloc[:3]).sum())
    tone = "go" if beat >= 2 else "stop"
    ui.card(f'<div class="tb-note"><b>Verdict</b>With your current swing rules, trading beat simply holding the '
            f'KSE-100 in {beat} of the last 3 years. '
            + ("Worth paper-testing further." if beat >= 2 else "Holding the market did better: keep most money in the core.")
            + '</div>', tone)
    st.dataframe(fmt_table(tab, pct_cols=("Winners", "Swing return", "KSE-100 buy and hold"),
                           rs_cols=("Avg per trade (Rs)", "Swing result (Rs)", "Holding instead (Rs)")),
                 hide_index=True, width="stretch")
    three = trades[trades["date"] >= last - 3 * yr] if trades is not None and not trades.empty else trades
    port = swing.one_at_a_time(three)
    eq = swing._equity(port, eng.index_hist, sw_cfg, last - 3 * yr, None)
    if not eq.empty:
        eq = eq.rename(columns={"Strategy": "Swing trades (Rs)", "KSE-100 buy and hold": "Holding KSE-100 (Rs)"})
        st.line_chart(eq.set_index("date")[["Swing trades (Rs)", "Holding KSE-100 (Rs)"]], color=["#FFB020", "#5CC8E8"])
    if three is not None and not three.empty:
        bs = swing.by_setup(three)[["setup", "trades", "win_rate", "avg", "net", "avg_days"]].rename(columns={
            "setup": "Entry type (all setups, last 3 years)", "trades": "Trades", "win_rate": "Winners",
            "avg": "Avg per trade (Rs)", "net": "Total (Rs)", "avg_days": "Avg days held"})
        st.dataframe(fmt_table(bs, pct_cols=("Winners",), rs_cols=("Avg per trade (Rs)", "Total (Rs)"),
                               dec_cols=("Avg days held",)), hide_index=True, width="stretch")
    st.caption(f"Uses the swing rules in the sidebar: buy at the close, one Rs {sw_cfg.capital:,.0f} position at a "
               "time, only when the KSE-100 is in an uptrend. On daily prices, a day that touches both the stop and "
               "the target counts as a stop. Uses today's KSE-100 members, which flatters stock-picking results a "
               "little. Change the swing rules in the sidebar to test ideas.")


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
                      eng.session_fraction(now, open_t, close_t), cfg, costs, kse_only,
                      mins_open=eng.mins_open(now, open_t) if is_open else None)
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

    core_section(prices)

    # ---- my position
    ui.panel("POS", "My open position", "Watched for stop, target, breakeven and the close")
    if eng.positions:
        for p in list(eng.positions):
            px = prices.get(p.symbol)
            if px is None or pd.isna(px):
                ui.empty(f"{p.symbol}: no live price right now.")
                continue
            pl = position_pl(p, px, costs, now)
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
            f4, f5, f6 = st.columns(3)
            stop = f4.number_input("Stop-loss (0 = auto)", 0.0, 100_000.0, 0.0, 0.01)
            target = f5.number_input("Target (0 = auto)", 0.0, 100_000.0, 0.0, 0.01)
            kind = f6.selectbox("Type", ["Swing (hold days)", "Intraday (sell today)"])
            if st.form_submit_button("Watch this position") and sym and entry > 0 and shares > 0:
                is_swing = kind.startswith("Swing")
                stop = stop or round(entry * (1 - (sw_max_stop if is_swing else stop_pct)), 2)
                target = target or round(entry + (sw_rr if is_swing else rr) * (entry - stop), 2)
                eng.add_position(Position(sym, entry, int(shares), stop, target,
                                          opened=now.strftime("%Y-%m-%d %H:%M"),
                                          kind="swing" if is_swing else "day"))
                st.rerun()

    swing_section(board, now, is_open)

    # ---- top setups
    top = scan[scan["actionable"]].head(5) if not scan.empty else scan
    ui.panel("SETUPS", "Intraday setups (paper only)",
             f"Momentum breakouts in the first {mom_window} min · alerts {'on' if intraday_alerts else 'off'}")
    st.caption("Intraday signals lost money on paper (17–23 Nov), so treat these as practice: the app paper-trades "
               "every one and the PAPER panel shows whether they're improving.")
    if scan.empty:
        ui.empty("No stocks pass your filters. Try lowering min volume or turning off KSE-100 only.")
    elif top.empty:
        ui.empty("No intraday setups right now. Momentum breakouts only count in the first "
                 f"{mom_window} minutes after the open.")
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
backtest_section()


def paper_section() -> None:
    eng.paper.connect(gh_token)
    everything = eng.paper.snapshot()
    trades = taken(everything)
    skipped = [t for t in everything if not t.taken]
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
        c1.download_button("Download journal (CSV)", journal_frame(everything).to_csv(index=False),
                           f"psx_paper_journal_{datetime.now(PKT):%Y-%m-%d}.csv", "text/csv", icon="⬇️",
                           help="Every trade with its entry conditions and how far it moved: taken and skipped.")

    if everything:
        ui.panel("JOURNAL", "What's working", "Results grouped by the conditions at entry, after costs and tax")
        by = st.segmented_control("Group by", list(GROUPS), default="Signal", key="journal_by") or "Signal"
        pool = everything if by == "Taken or skipped" else trades
        if pool:
            st.dataframe(breakdown(pool, by), hide_index=True, width="stretch")
        st.caption("**Avg best move** = how far the price went above the buy price before the trade closed. "
                   "If it is well below the target %, the signal has no follow-through. "
                   "**Reached +1× risk** = trades that got far enough to move the stop to breakeven. "
                   "Market mood and volume are recorded from 18 Nov onward. Give each group 10+ trades "
                   "before drawing conclusions.")
        if skipped:
            with st.expander(f"Skipped setups followed anyway ({len(skipped)}) — was skipping them right?"):
                s = paper_stats(skipped)
                st.markdown(f"{s['closed']} closed · {s['win_rate'] or 0:.0%} winners · net Rs {s['total']:+,.0f} · "
                            f"avg Rs {s['avg'] or 0:+,.0f} per trade. If this stays negative, the skip rule is "
                            "saving you money.")
                sdf = paper_frame(skipped).iloc[::-1].reset_index(drop=True)
                st.dataframe(ui.style_paper(sdf), hide_index=True, width="stretch",
                             height=min(320, 38 + 35 * len(sdf)))

    if eng.paper.gist_id:
        st.caption("Backed up to a private gist in your GitHub account, so restarts don't lose it. "
                   "A summary is pushed to your phone 10 minutes before the close.")
    elif eng.paper.remote_error:
        st.caption(f"{eng.paper.remote_error}. The log is kept on the app server meanwhile, and a restart wipes it.")
    else:
        st.caption("⚠️ Kept on the app server only, so a restart or update wipes new trades. Add GITHUB_TOKEN to "
                   "Secrets to keep the journal permanently (see README → Paper trades). A summary is pushed to "
                   "your phone 10 minutes before the close.")


paper_section()

with st.expander("How the signals and alerts work"):
    st.markdown(
        f"""
- **Strategy (24 Nov): core + satellite.** Most money (the core) is held in the market and not traded. The rest
  (the satellite) trades swing setups. Backtests on real PSX prices showed holding the KSE-100 beat the trading rules.
- **Swing breakout**: closes at a new 20-day high on 1.5× its usual volume, near the day's high, up less than 7%.
- **Swing pullback**: made a 20-day high in the last 2 weeks, dipped back to its 10-day average and closed above it, green on the day.
- **Swing filters**: only the strongest stocks by 3-month return, at least Rs 50m traded a day, and only when the
  KSE-100 is in an uptrend. Bought 3:00–3:27 pm; stop below the recent low (2–{sw_max_stop:.0%}); target {sw_rr}× the risk;
  stop to breakeven at +1× risk; sold at the close of day {sw_hold} if neither is hit.
- **Intraday momentum breakout** (paper only): up 1–7% today, volume at least 1.5× its normal pace, price in the top
  quarter of today's range and above VWAP, and only in the first {mom_window} minutes.
- **VWAP bounce** is now watch-only (0 winners in 36 paper trades).
- **Volume building**: volume 2× normal but price flat. Watch it; don't buy yet.
- **Overextended** (up 7%+) and **Falling** (down 3%+) are flagged so you avoid chasing or catching knives.
- **Stop-loss**: the tightest of VWAP, today's low, or {stop_pct:.1%} below entry, keeping at least 0.5% room,
  and wide enough that the profit at target is at least {min_rr:.2f}× the loss at stop after costs.
  If no stop within {stop_pct:.1%} manages that, the setup shows as **Costs too high - skip**. **Target**: {rr}× the risk.
- **VWAP** counts only after the app has watched a stock for 45 minutes. Before that (early morning, or after the
  app wakes up mid-day) there are no VWAP bounce signals, because a fresh VWAP sits right on the price.
- **Costs**: commission max(Rs {comm_min:.2f}/share, {comm:.2%}) + {stax:.0%} sales tax on the buy, and on the sell
  too unless it's the same day (KTrade charges intraday commission on the buy only), exchange/CDC fees,
  and {cgt:.0%} capital gains tax on profit.
- **Alerts** (ntfy push and/or WhatsApp, checked every minute in market hours): new buy setups (max 3 per scan, once per stock
  per day, paused while you hold a position unless you switch that on); for your position: stop hit → sell,
  target hit → sell, up 1× risk → move stop to breakeven, and 15 minutes before the close.
- **Paper trades**: every buy setup (once per stock per day, until 30 min before the close) is logged at its buy
  price and followed with the same rules: target, stop, breakeven at +1× risk, sold 15 min before the close.
  Net results include costs and tax. Use it to see whether the signals make money before trading them.
- **Journal**: each paper trade also records the time, the stock's move and volume, distance from VWAP, stop width
  and the market's mood when it was opened, plus the best and worst price it reached. Setups skipped as
  "Costs too high" are followed the same way, separately. Group the results in **What's working** or download
  the CSV to find which conditions make money and turn them into new rules.
- The background scanner runs while the app is awake. Open the site once each morning to wake it.
"""
    )
st.caption("Not financial advice. Data comes from the public PSX Data Portal and may be delayed or "
           "unavailable. Always confirm the price in your broker's app before placing an order.")
