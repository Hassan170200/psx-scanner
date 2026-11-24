"""Scanner rules, trade sizing and PSX cost maths. Pure functions, no network."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass
class Costs:
    commission_rate: float = 0.0025      # KTrade standard: 0.25% (Rs 20+ shares)
    min_comm_per_share: float = 0.05     # KTrade standard: Rs 0.05/share below Rs 20
    sales_tax: float = 0.16              # Punjab sales tax on commission
    exchange_fees: float = 0.000065      # PSX + NCCPL + SECP levies per side
    cdc_rate: float = 0.000036           # CDC per side
    cdc_min: float = 5.0                 # CDC minimum per side (Rs)
    cgt: float = 0.15                    # capital gains tax on profit
    same_day_sell_free: bool = True      # KTrade: a sell on the day of the buy pays no commission

    def per_side(self, shares: float, price: float, commission: bool = True) -> float:
        value = shares * price
        if shares <= 0 or value <= 0:
            return 0.0
        comm = max(shares * self.min_comm_per_share, value * self.commission_rate) if commission else 0.0
        return comm * (1 + self.sales_tax) + value * self.exchange_fees + max(self.cdc_min, value * self.cdc_rate)

    def sell_side(self, shares: float, price: float, same_day: bool) -> float:
        """Cost of the sell: no commission when sold the same day it was bought (KTrade)."""
        return self.per_side(shares, price, commission=not (same_day and self.same_day_sell_free))


@dataclass
class Settings:
    capital: float = 50_000
    position_pct: float = 1.0      # aggressive default: full capital per trade
    stop_pct: float = 0.02         # max distance of stop below entry
    reward_risk: float = 2.0       # target = entry + RR x risk
    min_price: float = 20.0
    min_volume: float = 200_000
    lot_size: int = 1
    min_rr: float = 1.0            # skip setups whose profit at target < this x the loss at stop, after costs
    momentum_window_min: int = 45  # intraday momentum breakouts count only this long after the open


# Bump when the buy/stop/target rules change, so the journal can compare rule sets fairly.
# 1 = up to 17 Nov 13:30 (tight stops, no VWAP warm-up); 2 = stops must pay for themselves + VWAP warm-up;
# 3 = 24 Nov: momentum breakouts only in the first 45 minutes, VWAP bounce dropped (0 winners in 36 paper
#     trades), same-day sells charged no commission.
RULES_VERSION = 3

ACTIONABLE = ("Momentum breakout",)
COSTLY = "Costs too high - skip"
LATE = "Momentum - too late"
VWAP_WATCH = "VWAP bounce - watch"
VWAP_WARMUP_MIN = 45  # minutes of snapshots before a stock's VWAP is trusted


def session_vwap(snaps: list[tuple[float, float, float]]) -> float | None:
    """VWAP from (timestamp, price, cumulative volume) snapshots taken while the app runs."""
    if len(snaps) < 3:
        return None
    pv = vol = 0.0
    for (_, p0, v0), (_, p1, v1) in zip(snaps, snaps[1:]):
        dv = v1 - v0
        if dv > 0:
            pv += dv * (p0 + p1) / 2
            vol += dv
    return pv / vol if vol > 0 else None


def classify(change_pct, rvol, pos_in_range, price, vwap, early: bool = True) -> str:
    if change_pct is None or pd.isna(change_pct):
        return "-"
    above_vwap = vwap is None or price > vwap
    if change_pct >= 7:
        return "Overextended - don't chase"
    if change_pct <= -3:
        return "Falling - avoid"
    near_high = pos_in_range is None or pd.isna(pos_in_range) or pos_in_range >= 0.75
    if rvol >= 1.5 and 1.0 <= change_pct < 7 and near_high and above_vwap:
        return "Momentum breakout" if early else LATE
    if vwap and change_pct >= 0 and rvol >= 1.0 and 0 <= (price - vwap) / vwap <= 0.006:
        return VWAP_WATCH
    if rvol >= 2 and abs(change_pct) < 1:
        return "Volume building - watch"
    return "-"


def score(row) -> float:
    s = min(row["rvol"], 5) * 20 + max(min(row["change_pct"], 6), -6) * 5
    if pd.notna(row.get("pos_in_range")):
        s += row["pos_in_range"] * 20
    if pd.notna(row.get("vwap")) and row["current"] > row["vwap"]:
        s += 10
    return round(s, 1)


def plan_trade(entry: float, low, vwap, cfg: Settings, costs: Costs) -> dict:
    """Entry/stop/target, share count and after-cost results for one setup."""
    floor_stop = entry * (1 - cfg.stop_pct)
    candidates = [floor_stop]
    if vwap and vwap < entry:
        candidates.append(vwap * 0.997)
    if low and not pd.isna(low) and low < entry:
        candidates.append(low * 0.998)
    # tightest structural stop (at least 0.5% room, no wider than the max stop) whose
    # profit at target still beats the loss at stop after costs; else the widest allowed
    stops = sorted({c for c in candidates if floor_stop <= c <= entry * 0.995}, reverse=True) or [floor_stop]
    plans = [_plan(entry, s, cfg, costs) for s in stops]
    for p in plans:
        if p["rr_after_costs"] is not None and p["rr_after_costs"] >= cfg.min_rr:
            return p
    return plans[-1]


def _plan(entry: float, stop: float, cfg: Settings, costs: Costs) -> dict:
    target = entry + cfg.reward_risk * (entry - stop)
    budget = cfg.capital * cfg.position_pct
    shares = math.floor(budget / entry / cfg.lot_size) * cfg.lot_size if entry > 0 else 0
    buy_cost = costs.per_side(shares, entry)
    sell_t = costs.sell_side(shares, target, same_day=True)  # intraday plan: sold the same day
    sell_s = costs.sell_side(shares, stop, same_day=True)
    gross_t = (target - entry) * shares
    pre_tax = gross_t - buy_cost - sell_t
    net_t = pre_tax * (1 - costs.cgt) if pre_tax > 0 else pre_tax
    loss_s = (stop - entry) * shares - buy_cost - sell_s
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "shares": shares,
        "position_rs": round(shares * entry, 0),
        "breakeven": round(entry + (buy_cost + costs.sell_side(shares, entry, same_day=True)) / shares, 2) if shares else None,
        "net_at_target": round(net_t, 0),
        "loss_at_stop": round(loss_s, 0),
        "rr_after_costs": round(net_t / abs(loss_s), 2) if loss_s else None,
    }


def build_scan(board: pd.DataFrame, avg_vol: dict, kse100: set, vwaps: dict,
               session_frac: float, cfg: Settings, costs: Costs, kse_only: bool,
               mins_open: float | None = None) -> pd.DataFrame:
    """`mins_open` = minutes since the open; momentum breakouts only count within
    cfg.momentum_window_min of it (None = no time limit, e.g. in tests)."""
    early = mins_open is None or mins_open <= cfg.momentum_window_min
    df = board.copy()
    for c in ("ldcp", "open", "high", "low", "change", "change_pct", "volume", "offer_price"):
        if c not in df.columns:
            df[c] = float("nan")
    df["change_pct"] = df["change_pct"].fillna(
        (df["current"] - df["ldcp"]) / df["ldcp"] * 100
    )
    df["volume"] = df["volume"].fillna(0)
    df["in_kse100"] = df["symbol"].isin(kse100)
    df["avg_vol_30d"] = df["symbol"].map(avg_vol)
    expected = df["avg_vol_30d"] * max(min(session_frac, 1.0), 0.1)
    df["rvol"] = (df["volume"] / expected).where(expected > 0).fillna(0).round(2)
    rng = df["high"] - df["low"]
    df["pos_in_range"] = ((df["current"] - df["low"]) / rng).where(rng > 0).round(2)
    df["vwap"] = df["symbol"].map(vwaps)

    df = df[(df["current"] >= cfg.min_price) & (df["volume"] >= cfg.min_volume)]
    if kse_only and kse100:
        df = df[df["in_kse100"]]
    if df.empty:
        return df

    df["signal"] = [
        classify(r.change_pct, r.rvol, r.pos_in_range, r.current, r.vwap if pd.notna(r.vwap) else None, early)
        for r in df.itertuples()
    ]
    df["score"] = df.apply(score, axis=1)
    plans, sigs = [], []
    for r in df.itertuples():
        entry = r.offer_price if pd.notna(r.offer_price) and r.offer_price > 0 else r.current
        p = (plan_trade(entry, r.low, r.vwap if pd.notna(r.vwap) else None, cfg, costs)
             if r.signal in ACTIONABLE else {})
        if p and (p["rr_after_costs"] is None or p["rr_after_costs"] < cfg.min_rr):
            sig = COSTLY  # a win wouldn't cover a loss after costs: not alerted, but the journal
                          # follows it as a skipped setup to check the skip rule is right
        else:
            sig = r.signal
        plans.append(p)
        sigs.append(sig)
    df["signal"] = sigs
    plan_df = pd.DataFrame(plans, index=df.index)
    df = pd.concat([df, plan_df], axis=1)
    for col in ("entry", "stop", "target", "shares", "position_rs", "breakeven", "net_at_target",
                "loss_at_stop", "rr_after_costs"):  # present even when nothing is actionable
        if col not in df.columns:
            df[col] = float("nan")
    df["actionable"] = df["signal"].isin(ACTIONABLE)
    return df.sort_values(["actionable", "score"], ascending=[False, False])
