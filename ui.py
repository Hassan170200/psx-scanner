"""Terminal-style look for the PSX scanner: CSS, status bar, ticker tape, panels and cards."""
from __future__ import annotations

import html

import pandas as pd
import streamlit as st

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
:root{
  --night:#070C14; --panel:#0E1622; --panel2:#121C2A; --rule:#1F2C3D;
  --amber:#FFB020; --up:#22C77A; --dn:#F0475B; --text:#D9E2EC; --muted:#7D8EA3; --note:#5CC8E8;
  --mono:'IBM Plex Mono',ui-monospace,monospace; --cond:'IBM Plex Sans Condensed','IBM Plex Sans',sans-serif;
}
html, body, .stApp, [data-testid="stSidebar"], button, input, textarea, select { font-family:'IBM Plex Sans',sans-serif; }
.stApp { background:var(--night); }
[data-testid="stHeader"] { background:transparent; }
.block-container { padding-top:2.2rem; padding-bottom:3rem; max-width:1400px; }
[data-testid="stSidebar"] { background:var(--panel); border-right:1px solid var(--rule); }
[data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 { font-family:var(--cond); font-weight:600; color:var(--amber); font-size:1rem; letter-spacing:.02em; }
h1,h2,h3 { font-family:var(--cond); }
[data-testid="stMetricValue"] { font-family:var(--mono); font-weight:500; font-size:1.35rem; }
[data-testid="stMetricLabel"] p { font-family:var(--cond); color:var(--muted); }
[data-testid="stVerticalBlockBorderWrapper"] { border-color:var(--rule) !important; border-radius:3px !important; background:var(--panel); }
[data-testid="stExpander"] details { border-color:var(--rule); border-radius:3px; background:var(--panel); }
.stButton > button, .stFormSubmitButton > button { border-radius:2px; border:1px solid var(--rule); background:var(--panel2);
  font-family:var(--cond); font-weight:500; color:var(--text); }
.stButton > button:hover, .stFormSubmitButton > button:hover { border-color:var(--amber); color:var(--amber); }
.stButton > button:focus-visible { outline:2px solid var(--amber); outline-offset:1px; }
[data-testid="stDataFrame"] { border:1px solid var(--rule); border-radius:3px; }
.stCaption, [data-testid="stCaptionContainer"] { color:var(--muted); }

/* status bar */
.tb-bar { display:flex; flex-wrap:wrap; align-items:stretch; border:1px solid var(--rule); background:var(--panel); margin-bottom:6px; }
.tb-brand { display:flex; align-items:center; gap:8px; padding:8px 14px; background:var(--amber); color:#120C00;
  font-family:var(--mono); font-weight:600; font-size:1.05rem; }
.tb-brand small { font-family:var(--cond); font-weight:500; font-size:.8rem; opacity:.75; }
.tb-cell { padding:6px 14px; border-left:1px solid var(--rule); display:flex; flex-direction:column; justify-content:center; min-width:96px; }
.tb-cell label { font-family:var(--cond); color:var(--muted); font-size:.72rem; }
.tb-cell b { font-family:var(--mono); font-weight:500; font-size:.95rem; color:var(--text); white-space:nowrap; }
.tb-cell.grow { flex:1; text-align:right; align-items:flex-end; }
.tb-open { color:var(--up) !important; } .tb-closed { color:var(--dn) !important; }
.tb-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:6px; vertical-align:1px; background:currentColor; }

/* ticker tape: the one moving thing on the page */
.tb-tape { overflow:hidden; border:1px solid var(--rule); border-top:0; background:#05080E; margin-bottom:18px; }
.tb-tape-track { display:flex; width:max-content; animation:tb-scroll var(--dur,90s) linear infinite; }
.tb-tape:hover .tb-tape-track { animation-play-state:paused; }
.tb-tk { font-family:var(--mono); font-size:.86rem; padding:7px 16px; border-right:1px solid #111a26; white-space:nowrap; }
.tb-tk b { color:var(--amber); font-weight:600; margin-right:8px; }
.tb-tk .p { color:var(--text); margin-right:6px; }
@keyframes tb-scroll { from { transform:translateX(0); } to { transform:translateX(-50%); } }
@media (prefers-reduced-motion: reduce) { .tb-tape { overflow-x:auto; } .tb-tape-track { animation:none; } }

.up { color:var(--up); } .dn { color:var(--dn); } .muted { color:var(--muted); } .mono { font-family:var(--mono); }

/* panel header */
.tb-ph { display:flex; align-items:baseline; gap:10px; margin:22px 0 8px; border-bottom:1px solid var(--rule); padding-bottom:6px; }
.tb-ph .key { font-family:var(--mono); font-size:.72rem; font-weight:600; background:var(--amber); color:#120C00; padding:2px 6px; }
.tb-ph .t { font-family:var(--cond); font-weight:600; font-size:1.2rem; color:var(--text); }
.tb-ph .m { margin-left:auto; font-family:var(--cond); font-size:.82rem; color:var(--muted); }

/* cards */
.tb-card { border:1px solid var(--rule); background:var(--panel); border-left:3px solid var(--rule); padding:10px 14px 12px; margin-bottom:6px; }
.tb-card.go { border-left-color:var(--up); } .tb-card.warn { border-left-color:var(--amber); } .tb-card.stop { border-left-color:var(--dn); }
.tb-card-h { display:flex; flex-wrap:wrap; align-items:baseline; gap:12px; margin-bottom:8px; }
.tb-sym { font-family:var(--mono); font-size:1.3rem; font-weight:600; color:var(--amber); }
.tb-tag { font-family:var(--cond); font-size:.82rem; padding:1px 8px; border:1px solid var(--rule); color:var(--text); }
.tb-tag.go { color:var(--up); border-color:#1d4d35; } .tb-tag.warn { color:var(--amber); border-color:#5a4313; } .tb-tag.stop { color:var(--dn); border-color:#5a1e27; }
.tb-px { margin-left:auto; font-family:var(--mono); font-size:1.05rem; }
.tb-sub { font-family:var(--cond); color:var(--muted); font-size:.85rem; }
.tb-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr)); gap:1px; background:var(--rule); border:1px solid var(--rule); }
.tb-grid > div { background:var(--panel2); padding:6px 10px; }
.tb-grid label { display:block; font-family:var(--cond); font-size:.74rem; color:var(--muted); }
.tb-grid span { font-family:var(--mono); font-size:1.02rem; font-variant-numeric:tabular-nums; }
.tb-grid small { display:block; font-family:var(--cond); font-size:.74rem; color:var(--muted); }
.tb-levels { font-family:var(--mono); font-size:.86rem; color:var(--text); margin:-2px 0 8px; }
.tb-levels i { font-style:normal; color:var(--muted); font-family:var(--cond); margin-right:4px; }
.tb-flags { margin:8px 0 0; padding:0; list-style:none; font-size:.84rem; color:#E9C77A; }
.tb-flags li::before { content:"▸ "; color:var(--amber); }
.tb-note { margin-top:8px; padding:6px 10px; border-left:2px solid var(--note); background:#0A1A24; color:#BFE6F3; font-size:.88rem; }
.tb-note b { color:var(--note); font-family:var(--cond); font-weight:600; margin-right:6px; }
.tb-empty { border:1px dashed var(--rule); padding:12px 14px; color:var(--muted); font-size:.9rem; }
.tb-grid span.txt { font-family:var(--cond); font-size:.98rem; }
@media (max-width: 640px) { .tb-ph .m { display:none; } .tb-px { margin-left:0; width:100%; } .block-container { padding-top:1.4rem; } }
</style>
"""


def esc(x) -> str:
    return html.escape(str(x))


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def fmt(x, d=2) -> str:
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return "–"


def signed(x, d=0, suffix="") -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "–"
    cls = "up" if v > 0 else "dn" if v < 0 else "muted"
    return f'<span class="{cls}">{v:+,.{d}f}{suffix}</span>'


def status_bar(is_open: bool, clock: str, up: int, down: int, setups: int, vwap_n: int, data_time: str) -> None:
    mk = ('<b class="tb-open"><span class="tb-dot"></span>Open</b>' if is_open
          else '<b class="tb-closed"><span class="tb-dot"></span>Closed</b>')
    st.markdown(f"""
<div class="tb-bar">
  <div class="tb-brand">PSX<small>terminal</small></div>
  <div class="tb-cell"><label>Market</label>{mk}</div>
  <div class="tb-cell"><label>Karachi time</label><b>{esc(clock)}</b></div>
  <div class="tb-cell"><label>Advancing / declining</label><b><span class="up">{up}</span> / <span class="dn">{down}</span></b></div>
  <div class="tb-cell"><label>Buy setups</label><b>{setups}</b></div>
  <div class="tb-cell"><label>VWAP tracked</label><b>{vwap_n}</b></div>
  <div class="tb-cell grow"><label>Prices from PSX data portal</label><b>{esc(data_time)}</b></div>
</div>""", unsafe_allow_html=True)


def ticker_tape(board: pd.DataFrame, n: int = 30) -> None:
    if board is None or board.empty or "volume" not in board.columns:
        return
    b = board.dropna(subset=["current"]).sort_values("volume", ascending=False).head(n)
    items = []
    for r in b.itertuples():
        chg = getattr(r, "change_pct", None)
        if chg is None or pd.isna(chg):
            ld = getattr(r, "ldcp", None)
            chg = (r.current / ld - 1) * 100 if ld and not pd.isna(ld) else 0.0
        cls, arrow = ("up", "▲") if chg > 0 else ("dn", "▼") if chg < 0 else ("muted", "■")
        items.append(f'<span class="tb-tk"><b>{esc(r.symbol)}</b><span class="p">{r.current:,.2f}</span>'
                     f'<span class="{cls}">{arrow} {chg:+.2f}%</span></span>')
    row = "".join(items)
    st.markdown(f'<div class="tb-tape" aria-label="Most active stocks"><div class="tb-tape-track" '
                f'style="--dur:{max(40, len(items) * 3)}s">{row}{row}</div></div>', unsafe_allow_html=True)


def panel(key: str, title: str, meta: str = "") -> None:
    st.markdown(f'<div class="tb-ph"><span class="key">{esc(key)}</span><span class="t">{esc(title)}</span>'
                f'<span class="m">{esc(meta)}</span></div>', unsafe_allow_html=True)


def cells(items: list[tuple[str, str, str]]) -> str:
    """items: (label, value_html, small_html)"""
    return '<div class="tb-grid">' + "".join(
        f'<div><label>{esc(l)}</label><span>{v}</span>{f"<small>{s}</small>" if s else ""}</div>'
        for l, v, s in items) + "</div>"


def setup_card(r) -> None:
    tone = "go" if r.signal == "Momentum breakout" else "warn"
    chg_cls = "up" if r.change_pct > 0 else "dn" if r.change_pct < 0 else "muted"
    warn = ""
    if r.rr_after_costs is not None and not pd.isna(r.rr_after_costs) and r.rr_after_costs < 1.0:
        warn = ('<ul class="tb-flags"><li>After costs and tax, profit at target is smaller than the loss at stop. '
                'Skip or widen the target.</li></ul>')
    st.markdown(f"""
<div class="tb-card {tone}">
  <div class="tb-card-h"><span class="tb-sym">{esc(r.symbol)}</span><span class="tb-tag {tone}">{esc(r.signal)}</span>
    <span class="tb-sub">volume {r.rvol:.1f}× normal</span>
    <span class="tb-px">{r.current:,.2f} <span class="{chg_cls}">{r.change_pct:+.2f}%</span></span></div>
  {cells([("Buy at", fmt(r.entry), ""), ("Stop-loss", f'<span class="dn">{fmt(r.stop)}</span>', ""),
          ("Target", f'<span class="up">{fmt(r.target)}</span>', ""),
          ("Shares", f"{int(r.shares):,}", f"Rs {r.position_rs:,.0f}"),
          ("Net at target", signed(r.net_at_target), "Rs, after costs and tax"),
          ("Loss at stop", signed(r.loss_at_stop), "Rs, after costs")])}
  {warn}
</div>""", unsafe_allow_html=True)


def position_card(p, px, pl) -> None:
    hit_stop, hit_tgt = px <= p.stop, px >= p.target
    tone = "stop" if hit_stop else "go" if hit_tgt else "warn"
    status = "Stop hit: sell" if hit_stop else "Target hit: sell" if hit_tgt else "Holding"
    move = (px / p.entry - 1) * 100
    st.markdown(f"""
<div class="tb-card {tone}">
  <div class="tb-card-h"><span class="tb-sym">{esc(p.symbol)}</span><span class="tb-tag {tone}">{status}</span>
    <span class="tb-sub">{p.shares:,} shares · bought {esc(p.opened)}</span>
    <span class="tb-px">{px:,.2f} {signed(move, 2, "%")}</span></div>
  {cells([("Bought at", fmt(p.entry), ""),
          ("Stop-loss", f'<span class="dn">{fmt(p.stop)}</span>', "moved to breakeven" if p.breakeven_moved else ""),
          ("Target", f'<span class="up">{fmt(p.target)}</span>', ""),
          ("P/L now", signed(pl), "Rs, after costs, before tax")])}
</div>""", unsafe_allow_html=True)


def call_card(c: dict, ev: dict) -> None:
    v = ev.get("verdict", "")
    tone = "go" if v.startswith("✅") else "stop" if v.startswith("❌") else "warn"
    tps = " / ".join(f"{t:g}" for t in c.get("targets") or []) or "–"
    zone = (f"≤ {c.get('entry_high')}" if c.get("or_below")
            else f"{c.get('entry_low')}" if c.get("entry_low") == c.get("entry_high")
            else f"{c.get('entry_low')}–{c.get('entry_high')}")
    meta = " · ".join(x for x in [c.get("source", ""), c.get("date", ""), c.get("timeframe") or ""] if x)
    grid = [("Now", fmt(ev.get("price")) if ev.get("price") else "–", esc(ev.get("status", ""))),
            ("Since call", f'<span class="txt">{esc(ev.get("outcome", "–"))}</span>', "")]
    if ev.get("shares"):
        grid += [("Size for your risk", f"{ev['shares']:,}", f"shares · Rs {ev['position_rs']:,.0f}"),
                 ("At TP1", signed(ev.get("net_tp1")), "Rs, after costs and tax"),
                 ("At final target", signed(ev.get("net_last")), "Rs, after costs and tax"),
                 ("At stop", signed(ev.get("loss_at_stop")), "Rs, after costs")]
    flags = "".join(f"<li>{esc(f)}</li>" for f in ev.get("flags", []))
    note = f'<div class="tb-note"><b>Claude</b>{esc(c["claude_note"])}</div>' if c.get("claude_note") else ""
    st.markdown(f"""
<div class="tb-card {tone}">
  <div class="tb-card-h"><span class="tb-sym">{esc(c['symbol'])}</span><span class="tb-tag {tone}">{esc(v)}</span>
    <span class="tb-sub">{esc(meta)}</span></div>
  <div class="tb-levels"><i>Buy</i>{esc(zone)} &nbsp; <i>Stop</i><span class="dn">{esc(c.get('stop'))}</span> &nbsp;
    <i>Targets</i><span class="up">{esc(tps)}</span></div>
  {cells(grid)}
  {f'<ul class="tb-flags">{flags}</ul>' if flags else ''}
  {note}
</div>""", unsafe_allow_html=True)


def empty(text: str) -> None:
    st.markdown(f'<div class="tb-empty">{esc(text)}</div>', unsafe_allow_html=True)


def style_scan(show: pd.DataFrame):
    """Text-formatted scanner table (blank cells show a dash) with direction colours."""
    num = show.copy()
    for col in num.columns:
        if col not in ("Symbol", "Signal"):
            num[col] = pd.to_numeric(num[col], errors="coerce")
    fmts = {"Score": "{:.0f}", "Price": "{:,.2f}", "Chg %": "{:+.2f}", "Vol vs normal": "{:.1f}×",
            "Volume": "{:,.0f}", "VWAP": "{:,.2f}", "Buy": "{:,.2f}", "Stop": "{:,.2f}", "Target": "{:,.2f}",
            "Shares": "{:,.0f}", "Net @ target (Rs)": "{:+,.0f}", "Loss @ stop (Rs)": "{:+,.0f}"}
    disp = num.copy()
    for col, f in fmts.items():
        if col in disp.columns:
            disp[col] = num[col].map(lambda v, f=f: "–" if pd.isna(v) else f.format(v))
    styles = pd.DataFrame("", index=num.index, columns=num.columns)
    for col in fmts:
        if col in styles.columns:
            styles[col] = "text-align:right"
    for col in ("Chg %", "Net @ target (Rs)", "Loss @ stop (Rs)"):
        styles[col] = [("text-align:right;color:#22C77A" if v > 0 else "text-align:right;color:#F0475B" if v < 0
                        else "text-align:right") if not pd.isna(v) else "text-align:right" for v in num[col]]
    sig = {"Momentum breakout": "color:#22C77A;font-weight:600", "VWAP bounce": "color:#22C77A",
           "Volume building - watch": "color:#FFB020"}
    styles["Signal"] = [sig.get(v, "color:#F0475B" if str(v).startswith(("Falling", "Over")) else "color:#7D8EA3")
                        for v in num["Signal"]]
    styles["Symbol"] = "color:#FFB020;font-weight:600"
    return disp.style.apply(lambda _: styles, axis=None)
