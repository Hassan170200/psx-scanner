"""Swing strategy (rules v3): buy strong KSE-100 stocks near the close, hold 2-5 days.

Pure functions on daily bars (date, open, high, low, close, volume), no network.

Two entry types, both bought at the day's close (in practice 3:00-3:25 pm):
- breakout: closes at a new 20-day high on >= 1.5x its 20-day average volume, in the
  top quarter of the day's range, up less than 7% on the day.
- pullback: made a 20-day high in the last 10 sessions, today's low came back to the
  10-day average (within 1%), it closed back above that average and above the
  20-day average, and the close was up on the day.

Shared filters:
- market: KSE-100 closed above its 20-day average, and the 20-day is above the 50-day.
- strength: among the 20 KSE-100 stocks with the best 63-day (3-month) return.
- liquidity: 20-day average turnover of at least Rs 50m and price >= Rs 20.

Trade plan:
- stop: below the breakout day's low (breakout) or the 3-day low (pullback), minus
  0.5%; at least 2% and at most 5% below the buy price.
- target: 2x the risk above the buy price.
- once a day's high reaches +1x risk, the stop moves to the buy price + costs.
- sold at the close of the 4th trading day after buying if neither is hit.
On daily bars the order of moves inside a day is unknown: if a day touches both the
stop and the target, the stop is assumed to come first. A gap through the stop or
target fills at the open.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from signals import Costs

SETUPS = ("breakout", "pullback")
LABEL = {"breakout": "Swing breakout", "pullback": "Swing pullback"}


@dataclass
class SwingSettings:
    capital: float = 50_000
    lookback_high: int = 20          # breakout = close above the prior N-day high
    vol_mult: float = 1.5            # breakout volume vs 20-day average
    min_range_pos: float = 0.75      # close in the top quarter of the day's range
    max_day_gain: float = 7.0        # skip if already up this much today (%)
    pullback_window: int = 10        # a 20-day high within the last N sessions
    pullback_tol: float = 0.01       # low within 1% of the 10-day average
    top_n: int = 20                  # strongest N by 63-day return
    min_turnover: float = 50e6       # Rs per day (20-day average)
    min_price: float = 20.0
    min_stop: float = 0.02
    max_stop: float = 0.05
    reward_risk: float = 2.0
    hold_days: int = 4               # sell at the close of this trading day after entry
    market_filter: bool = True
    setups: tuple = SETUPS


# ---------------------------------------------------------------- data prep
def clean_history(df: pd.DataFrame) -> pd.DataFrame:
    """Sort, drop bad rows and back-adjust for splits/bonus issues.

    PSX limits a day's move to 10% (or Rs 1, whichever is larger), so an overnight jump
    bigger than that is a corporate action (split, bonus, rights): earlier prices are
    scaled so the series is continuous and stops aren't triggered by the adjustment."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    d = df[["date", "open", "high", "low", "close", "volume"]].copy()
    d["date"] = pd.to_datetime(d["date"])
    for c in ("open", "high", "low", "close", "volume"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["close"]).query("close > 0").drop_duplicates("date").sort_values("date")
    for c in ("open", "high", "low"):
        d[c] = d[c].where(d[c] > 0, d["close"])
    d["volume"] = d["volume"].fillna(0)
    d = d.reset_index(drop=True)
    prev = d["close"].shift(1)
    limit = np.maximum(0.115, 1.05 / prev)
    ratio = d["open"] / prev
    jump = (ratio - 1).abs() > limit
    factor = pd.Series(1.0, index=d.index)
    for i in d.index[jump.fillna(False)]:
        r = ratio.iloc[i]
        if r > 0:
            factor.iloc[:i] *= r
    if (factor != 1).any():
        for c in ("open", "high", "low", "close"):
            d[c] = d[c] * factor
        d["volume"] = d["volume"] / factor
    return d


def add_indicators(d: pd.DataFrame, cfg: SwingSettings) -> pd.DataFrame:
    d = d.copy()
    c = d["close"]
    d["sma10"] = c.rolling(10).mean()
    d["sma20"] = c.rolling(20).mean()
    d["sma50"] = c.rolling(50).mean()
    d["hh_prior"] = d["high"].shift(1).rolling(cfg.lookback_high).max()
    d["avgvol"] = d["volume"].shift(1).rolling(20).mean()
    d["turnover"] = (d["close"] * d["volume"]).shift(1).rolling(20).mean()
    d["ret63"] = c / c.shift(63) - 1
    d["chg"] = (c / c.shift(1) - 1) * 100
    rng = d["high"] - d["low"]
    d["range_pos"] = ((c - d["low"]) / rng).where(rng > 0, 1.0)
    d["new_high"] = c > d["hh_prior"]
    d["recent_high"] = d["new_high"].astype(int).shift(1).rolling(cfg.pullback_window).max().fillna(0) > 0
    d["low3"] = d["low"].rolling(3).min()
    return d


def market_ok(index_df: pd.DataFrame) -> pd.Series:
    """True on days the KSE-100 is in an uptrend (close > 20-day avg > 50-day avg)."""
    d = clean_history(index_df)
    c = d["close"]
    s20, s50 = c.rolling(20).mean(), c.rolling(50).mean()
    return pd.Series(((c > s20) & (s20 > s50)).values, index=d["date"])


# ---------------------------------------------------------------- signals
def setup_on(row, cfg: SwingSettings) -> str | None:
    """Which entry type (if any) this day's bar is."""
    if any(pd.isna(row[k]) for k in ("sma20", "hh_prior", "avgvol", "sma10")):
        return None
    if row["close"] < cfg.min_price or not (row["turnover"] >= cfg.min_turnover):
        return None
    if ("breakout" in cfg.setups and row["new_high"] and row["avgvol"] > 0
            and row["volume"] >= cfg.vol_mult * row["avgvol"]
            and row["range_pos"] >= cfg.min_range_pos and row["chg"] < cfg.max_day_gain):
        return "breakout"
    if ("pullback" in cfg.setups and row["recent_high"] and not row["new_high"]
            and row["low"] <= row["sma10"] * (1 + cfg.pullback_tol) and row["close"] > row["sma10"]
            and row["close"] > row["sma20"] and row["chg"] > 0):
        return "pullback"
    return None


def plan(setup: str, row, cfg: SwingSettings, costs: Costs) -> dict:
    entry = float(row["close"])
    structural = (row["low"] if setup == "breakout" else row["low3"]) * 0.995
    stop = min(structural, entry * (1 - cfg.min_stop))
    stop = max(stop, entry * (1 - cfg.max_stop))
    risk = entry - stop
    target = entry + cfg.reward_risk * risk
    shares = math.floor(cfg.capital / entry) if entry > 0 else 0
    buy = costs.per_side(shares, entry)
    win = (target - entry) * shares - buy - costs.per_side(shares, target)
    win = win * (1 - costs.cgt) if win > 0 else win
    loss = (stop - entry) * shares - buy - costs.per_side(shares, stop)
    return {"entry": round(entry, 2), "stop": round(stop, 2), "target": round(target, 2), "shares": shares,
            "risk": risk, "stop_pct": round(risk / entry * 100, 2),
            "net_at_target": round(win, 0), "loss_at_stop": round(loss, 0)}


def candidates(panel: dict, mkt: pd.Series | None, cfg: SwingSettings, costs: Costs) -> pd.DataFrame:
    """Every (date, symbol) swing setup in the history, ranked by 3-month strength."""
    frames = []
    for sym, d in panel.items():
        if d.empty:
            continue
        x = d[["date", "close", "ret63"]].copy()
        x["symbol"] = sym
        frames.append(x)
    if not frames:
        return pd.DataFrame()
    allr = pd.concat(frames)
    allr["rank"] = allr.groupby("date")["ret63"].rank(ascending=False, method="first")
    rank = allr.set_index(["date", "symbol"])["rank"]
    rows = []
    for sym, d in panel.items():
        for i, row in enumerate(d.itertuples(index=False)):
            r = row._asdict()
            s = setup_on(r, cfg)
            if not s:
                continue
            dt = r["date"]
            rk = rank.get((dt, sym), np.nan)
            if pd.isna(rk) or rk > cfg.top_n:
                continue
            if cfg.market_filter and mkt is not None and not bool(mkt.get(dt, False)):
                continue
            p = plan(s, r, cfg, costs)
            rows.append({"date": dt, "symbol": sym, "setup": s, "rank": int(rk), "i": i,
                         "ret63": r["ret63"], **p})
    return pd.DataFrame(rows).sort_values(["date", "rank"]).reset_index(drop=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------- trade simulation
def simulate(d: pd.DataFrame, i: int, p: dict, cfg: SwingSettings, costs: Costs) -> dict:
    """Follow one trade bought at the close of bar i. Returns exit details and net rupees."""
    entry, stop, target, shares = p["entry"], p["stop"], p["target"], p["shares"]
    risk = entry - stop
    be_price = entry + 2 * costs.per_side(shares, entry) / max(shares, 1)
    be = False
    exit_px, reason, j = None, "open", i
    n = len(d)
    for k in range(1, cfg.hold_days + 1):
        j = i + k
        if j >= n:
            break
        o, h, l, c = (float(d.at[j, x]) for x in ("open", "high", "low", "close"))
        cur_stop = max(stop, be_price) if be else stop
        if o <= cur_stop:
            exit_px, reason = o, ("breakeven" if be else "stop")
            break
        if o >= target:
            exit_px, reason = o, "target"
            break
        if l <= cur_stop:
            exit_px, reason = cur_stop, ("breakeven" if be else "stop")
            break
        if h >= target:
            exit_px, reason = target, "target"
            break
        if h >= entry + risk:
            be = True
        if k == cfg.hold_days:
            exit_px, reason = c, "time"
    days = j - i
    if exit_px is None:  # still open at the end of the data
        last = d.iloc[min(j, n - 1)]
        return {"exit_date": last["date"], "exit": float(last["close"]), "reason": "open", "days": days,
                "net": None, "j": min(j, n - 1)}
    pre = (exit_px - entry) * shares - costs.per_side(shares, entry) - costs.per_side(shares, exit_px)
    net = pre * (1 - costs.cgt) if pre > 0 else pre
    return {"exit_date": d.at[j, "date"], "exit": round(float(exit_px), 2), "reason": reason, "days": days,
            "net": round(net, 0), "j": j}


@dataclass
class BacktestResult:
    trades: pd.DataFrame                      # every setup, each followed on its own
    portfolio: pd.DataFrame                   # one Rs 50k position at a time
    equity: pd.DataFrame                      # date, strategy equity, KSE-100 buy-and-hold
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    notes: list = field(default_factory=list)


def backtest(histories: dict, index_df: pd.DataFrame, cfg: SwingSettings | None = None,
             costs: Costs | None = None, start=None, end=None) -> BacktestResult:
    cfg, costs = cfg or SwingSettings(), costs or Costs()
    panel = {s: add_indicators(clean_history(h), cfg).reset_index(drop=True) for s, h in histories.items()}
    mkt = market_ok(index_df) if index_df is not None and not index_df.empty else None
    cand = candidates(panel, mkt, cfg, costs)
    start = pd.Timestamp(start) if start is not None else None
    end = pd.Timestamp(end) if end is not None else None
    if not cand.empty:
        if start is not None:
            cand = cand[cand["date"] >= start]
        if end is not None:
            cand = cand[cand["date"] <= end]
    sims = []
    for r in cand.itertuples(index=False):
        res = simulate(panel[r.symbol], r.i, r._asdict(), cfg, costs)
        sims.append({**r._asdict(), **res})
    trades = pd.DataFrame(sims)
    # one position at a time: when flat, take the strongest setup of the day
    port, free_after = [], None
    if not trades.empty:
        for dt, day in trades.groupby("date", sort=True):
            if free_after is not None and dt < free_after:
                continue
            pick = day.sort_values("rank").iloc[0]
            port.append(pick)
            free_after = pick["exit_date"] if pick["reason"] != "open" else pd.Timestamp.max
            if pick["reason"] != "open" and pick["exit_date"] == dt:
                free_after = dt + pd.Timedelta(days=1)
    portfolio = pd.DataFrame(port).reset_index(drop=True)
    equity = _equity(portfolio, index_df, cfg, start, end)
    return BacktestResult(trades, portfolio, equity, start, end)


def _equity(portfolio: pd.DataFrame, index_df, cfg: SwingSettings, start, end) -> pd.DataFrame:
    idx = clean_history(index_df) if index_df is not None else pd.DataFrame()
    if idx.empty:
        return pd.DataFrame()
    if start is not None:
        idx = idx[idx["date"] >= start]
    if end is not None:
        idx = idx[idx["date"] <= end]
    if idx.empty:
        return pd.DataFrame()
    eq = pd.DataFrame({"date": idx["date"].values})
    eq["KSE-100 buy and hold"] = cfg.capital * idx["close"].values / idx["close"].values[0]
    pnl = pd.Series(0.0, index=eq["date"])
    if not portfolio.empty:
        closed = portfolio[portfolio["reason"] != "open"]
        for r in closed.itertuples():
            k = pnl.index.searchsorted(r.exit_date)
            if k < len(pnl):
                pnl.iloc[k] += r.net
    eq["Strategy"] = cfg.capital + pnl.cumsum().values
    return eq


def summary(trades: pd.DataFrame) -> dict:
    """Headline numbers for a set of closed trades."""
    t = trades[trades["reason"] != "open"] if not trades.empty else trades
    if t.empty:
        return {"trades": 0}
    nets = t["net"].astype(float)
    wins, losses = nets[nets > 0], nets[nets <= 0]
    eq = nets.cumsum()
    dd = (eq - eq.cummax().clip(lower=0)).min()
    streak = worst = 0
    for v in nets:
        streak = streak + 1 if v <= 0 else 0
        worst = max(worst, streak)
    return {
        "trades": int(len(t)), "win_rate": float((nets > 0).mean()), "net": float(nets.sum()),
        "avg": float(nets.mean()), "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "max_drawdown": float(dd), "worst_streak": int(worst), "avg_days": float(t["days"].mean()),
        "exits": t["reason"].value_counts().to_dict(),
    }


def by_setup(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for s in SETUPS:
        sm = summary(trades[trades["setup"] == s]) if not trades.empty else {"trades": 0}
        rows.append({"setup": s, **sm})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- live (today)
def today_bar(hist: pd.DataFrame, live: dict, today) -> pd.DataFrame:
    """History plus today's bar built from live prices (replacing a partial row for today)."""
    d = clean_history(hist)
    today = pd.Timestamp(today).normalize()
    d = d[d["date"] < today]
    cur = live.get("current")
    if cur is None or pd.isna(cur) or cur <= 0:
        return d
    row = {"date": today, "open": live.get("open") or cur, "high": max(live.get("high") or cur, cur),
           "low": min(live.get("low") or cur, cur), "close": cur, "volume": live.get("volume") or 0}
    row = {k: (cur if (k != "date" and k != "volume" and (v is None or pd.isna(v) or v <= 0)) else v)
           for k, v in row.items()}
    return pd.concat([d, pd.DataFrame([row])], ignore_index=True)


def live_candidates(histories: dict, board: pd.DataFrame, index_df: pd.DataFrame, index_live: float | None,
                    today, cfg: SwingSettings | None = None, costs: Costs | None = None) -> tuple[pd.DataFrame, bool]:
    """Today's swing setups using live prices as today's bar. Returns (setups, market_ok).
    market_ok uses today's index close when given, else the last close. Volume is the volume
    so far, so the breakout volume test is conservative before the close."""
    cfg, costs = cfg or SwingSettings(), costs or Costs()
    live = board.set_index("symbol").to_dict("index") if board is not None and not board.empty else {}
    t = pd.Timestamp(today).normalize()
    last_rows = []
    for s, h in histories.items():
        if s not in live:
            continue
        d = add_indicators(today_bar(h, live[s], t), cfg)
        if d.empty or d["date"].iloc[-1] != t:
            continue
        r = d.iloc[-1].to_dict()
        r["symbol"] = s
        last_rows.append(r)
    idx = clean_history(index_df)
    if index_live:
        idx = today_bar(idx, {"current": index_live}, t)
    mkt = market_ok(idx)
    upto = mkt[mkt.index <= t]
    ok = bool(upto.iloc[-1]) if len(upto) else False
    if not last_rows:
        return pd.DataFrame(), ok
    today_df = pd.DataFrame(last_rows)
    today_df["rank"] = today_df["ret63"].rank(ascending=False, method="first")
    rows = []
    for r in today_df.to_dict("records"):
        st = setup_on(r, cfg)
        if not st or pd.isna(r["rank"]) or r["rank"] > cfg.top_n:
            continue
        rows.append({"date": t, "symbol": r["symbol"], "setup": st, "rank": int(r["rank"]),
                     "ret63": r["ret63"], "chg": r["chg"], **plan(st, r, cfg, costs)})
    out = pd.DataFrame(rows)
    return (out.sort_values("rank").reset_index(drop=True) if not out.empty else out), ok


# ---------------------------------------------------------------- reports
def prepare(histories: dict, index_df: pd.DataFrame, cfg: SwingSettings | None = None,
            costs: Costs | None = None) -> pd.DataFrame:
    """Every setup in the whole history, each followed to its exit (one pass, sliced later)."""
    cfg, costs = cfg or SwingSettings(), costs or Costs()
    return backtest(histories, index_df, cfg, costs).trades


def one_at_a_time(trades: pd.DataFrame) -> pd.DataFrame:
    """The trades you'd actually take with one position: when flat, the strongest setup of the day."""
    port, free_after = [], None
    if trades is None or trades.empty:
        return pd.DataFrame()
    for dt, day in trades.groupby("date", sort=True):
        if free_after is not None and dt <= free_after:
            continue
        pick = day.sort_values("rank").iloc[0]
        port.append(pick)
        free_after = pick["exit_date"] if pick["reason"] != "open" else pd.Timestamp.max
    return pd.DataFrame(port).reset_index(drop=True)


def index_return(index_df: pd.DataFrame, start, end=None) -> float | None:
    d = clean_history(index_df)
    d = d[(d["date"] >= pd.Timestamp(start)) & ((d["date"] <= pd.Timestamp(end)) if end is not None else True)]
    if len(d) < 2:
        return None
    return float(d["close"].iloc[-1] / d["close"].iloc[0] - 1)


def period_table(trades: pd.DataFrame, index_df: pd.DataFrame, periods: list, capital: float) -> pd.DataFrame:
    """Per period: the one-position swing result vs simply holding the KSE-100."""
    rows = []
    for label, start, end in periods:
        t = trades
        if t is not None and not t.empty:
            t = t[(t["date"] >= pd.Timestamp(start)) & ((t["date"] <= pd.Timestamp(end)) if end is not None else True)]
        port = one_at_a_time(t) if t is not None and not t.empty else pd.DataFrame()
        sm = summary(port) if not port.empty else {"trades": 0}
        idx_ret = index_return(index_df, start, end)
        net = sm.get("net", 0.0) if sm.get("trades") else 0.0
        rows.append({"Period": label, "Swing trades": sm.get("trades", 0),
                     "Winners": sm.get("win_rate"), "Avg per trade (Rs)": sm.get("avg"),
                     "Swing result (Rs)": net, "Swing return": net / capital,
                     "KSE-100 buy and hold": idx_ret,
                     "Holding instead (Rs)": (idx_ret * capital) if idx_ret is not None else None})
    return pd.DataFrame(rows)


def index_status(index_df: pd.DataFrame) -> dict:
    """Where the KSE-100 stands: level, trend and recent returns."""
    d = clean_history(index_df)
    if len(d) < 60:
        return {}
    c = d["close"]
    last = float(c.iloc[-1])

    def back(n):
        return float(last / c.iloc[-1 - n] - 1) if len(c) > n else None

    s20, s50 = float(c.rolling(20).mean().iloc[-1]), float(c.rolling(50).mean().iloc[-1])
    s200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None
    hi = float(c.tail(250).max())
    return {"date": d["date"].iloc[-1], "level": last, "sma20": s20, "sma50": s50, "sma200": s200,
            "uptrend": last > s20 > s50, "ret_1m": back(21), "ret_3m": back(63), "ret_1y": back(245),
            "from_high": last / hi - 1}


def core_basket(weights: pd.DataFrame, prices: dict, amount: float, n: int = 8) -> pd.DataFrame:
    """The n biggest KSE-100 stocks by index weight, with the money split in proportion to weight."""
    if weights is None or weights.empty or amount <= 0:
        return pd.DataFrame()
    w = weights.dropna(subset=["weight"]).sort_values("weight", ascending=False).head(n).copy()
    w["share"] = w["weight"] / w["weight"].sum()
    w["price"] = w["symbol"].map(prices)
    w = w.dropna(subset=["price"])
    w = w[w["price"] > 0]
    w["rupees"] = amount * w["share"]
    w["shares"] = (w["rupees"] // w["price"]).astype(int)
    w["cost"] = w["shares"] * w["price"]
    return w[["symbol", "weight", "share", "price", "shares", "cost"]].reset_index(drop=True)
