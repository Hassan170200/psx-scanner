"""Broker calls: load, parse, and sense-check trade calls from WhatsApp channels.

Calls live in a small JSON file (a secret GitHub gist) that Claude updates hourly
from the WhatsApp channels. Every number here is computed from PSX data at view
time, so a call's status and verdict stay current between updates.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import pandas as pd
import requests

from signals import Costs

RISK_BUDGET = 1_300  # Rs lost if the stop is hit: sizes every call


# ---------------------------------------------------------------- load / parse
def load_calls(url: str) -> tuple[list[dict], str]:
    """Calls from the shared data file. Returns (calls, last-updated text)."""
    if not url:
        return [], ""
    r = requests.get(url, params={"t": int(time.time() // 60)}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return list(data.get("calls", [])), str(data.get("updated", ""))


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_message(text: str, source: str = "Pasted", date: str | None = None) -> dict | None:
    """Turn a broker's WhatsApp call into a call dict. None if it isn't a call."""
    t = re.sub(r"\s+", " ", text or "").strip()
    sym = (re.search(r"\b([A-Z][A-Z0-9]{1,7})\s*[–-]+\s*(?:Swing\s+)?Trade", t)
           or re.search(r"Trade Idea\s*[–-]\s*([A-Z][A-Z0-9]{1,7})", t)
           or re.search(r"\bBuy\s+([A-Z][A-Z0-9]{1,7})\b", t)
           or re.search(r"^\W*([A-Z][A-Z0-9]{1,7})\b", t))
    if not sym:
        return None
    ez = re.search(r"(?:Entry Zone|Buy Zone|Ideal Range|Buy at)\W*(?:Rs\.?\s*)?([\d.,]+)\s*(?:(?:[–-]|to)\s*([\d.,]+))?", t, re.I)
    cmp_ = re.search(r"(?:Current Rate|CMP)\W*([\d.,]+)", t, re.I)
    sl = re.search(r"(?:Stop\s*Loss|Risk Level \(SL\)|\bSL)\W*([\d.,]+)", t, re.I)
    tps = [_num(m) for m in re.findall(r"(?:TP\s?\d|Final TP|Target\s?\d?)\s*(?:→)?\s*:?\s*([\d.,]+)", t, re.I)]
    tps = sorted({x for x in tps if x})
    lo = _num(ez.group(1)) if ez else (_num(cmp_.group(1)) if cmp_ else None)
    hi = _num(ez.group(2)) if ez and ez.group(2) else lo
    or_below = bool(ez and re.match(r"\s*or below", t[ez.end():], re.I))
    if lo and hi and lo > hi:
        lo, hi = hi, lo
    if not tps and not sl:
        return None
    d = re.search(r"Date:?\s*(\d{1,2}\s+[A-Za-z]+\s+20\d\d)", t)
    tf = re.search(r"Time Frame:?\s*([A-Za-z\- ]+?)(?:\s+Strategy|\s+Note|$)", t, re.I)
    if d:
        try:
            date = datetime.strptime(d.group(1), "%d %B %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return {
        "id": f"{sym.group(1)}-{date or datetime.now().strftime('%Y-%m-%d')}",
        "source": source,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "symbol": sym.group(1),
        "entry_low": lo, "entry_high": hi, "or_below": or_below,
        "stop": _num(sl.group(1)) if sl else None,
        "targets": tps,
        "timeframe": tf.group(1).strip() if tf else None,
        "claude_note": "",
        "raw": t[:600],
    }


# ---------------------------------------------------------------- evaluate
def outcome_since(hist: pd.DataFrame | None, call: dict) -> str:
    """What happened after the call, from daily highs/lows (stop counted first on a tie)."""
    if hist is None or hist.empty or not call.get("date"):
        return "No history"
    h = hist[hist["date"] >= pd.Timestamp(call["date"])].sort_values("date")
    stop, tps = call.get("stop"), call.get("targets") or []
    entry_hi = call.get("entry_high") or call.get("entry_low")
    filled = False
    reached = 0
    for r in h.itertuples():
        if not filled:
            if entry_hi and r.low <= entry_hi:
                filled = True
            else:
                continue
        if stop and r.low <= stop:
            return "Stop hit" if reached == 0 else f"TP{reached} then stopped"
        while reached < len(tps) and r.high >= tps[reached]:
            reached += 1
    if not filled:
        return "Not filled yet"
    return f"TP{reached} hit" if reached else "Open"


def trend(hist: pd.DataFrame | None) -> dict:
    if hist is None or len(hist) < 30:
        return {}
    c = hist.sort_values("date")["close"]
    out = {"chg_20d": (c.iloc[-1] / c.iloc[-21] - 1) * 100}
    if len(c) >= 50:
        out["above_sma50"] = c.iloc[-1] > c.tail(50).mean()
    if len(c) >= 200:
        out["above_sma200"] = c.iloc[-1] > c.tail(200).mean()
    v = hist.sort_values("date")["volume"].tail(20)
    out["avg_vol_20d"] = float(v.mean())
    return out


def evaluate(call: dict, price: float | None, hist: pd.DataFrame | None, costs: Costs,
             risk_budget: float = RISK_BUDGET) -> dict:
    lo, hi = call.get("entry_low"), call.get("entry_high") or call.get("entry_low")
    stop, tps = call.get("stop"), call.get("targets") or []
    if call.get("or_below"):
        entry = hi
        lo = stop * 1.001 if stop else lo
    else:
        entry = (lo + hi) / 2 if lo and hi else (lo or price)
    res = {"entry": entry, "price": price, "outcome": outcome_since(hist, call), **trend(hist)}
    flags, verdict = [], "✅ Reasonable"

    if not stop or not entry or stop >= entry:
        res.update(verdict="❌ Skip", flags=["No usable stop-loss"], shares=0)
        return res
    risk_ps = entry - stop
    res["stop_pct"] = risk_ps / entry * 100
    rough = max(int(risk_budget // risk_ps), 1)
    cost_ps = (costs.per_side(rough, entry) + costs.per_side(rough, stop)) / rough
    shares = int(risk_budget // (risk_ps + cost_ps))
    res["shares"] = shares
    res["position_rs"] = shares * entry

    def net(target):
        g = (target - entry) * shares - costs.per_side(shares, entry) - costs.per_side(shares, target)
        return g * (1 - costs.cgt) if g > 0 else g
    loss = (stop - entry) * shares - costs.per_side(shares, entry) - costs.per_side(shares, stop)
    res["loss_at_stop"] = loss
    if tps:
        res["net_tp1"] = net(tps[0])
        res["net_last"] = net(tps[-1])
        res["rr_tp1"] = res["net_tp1"] / abs(loss) if loss else None
        res["rr_last"] = res["net_last"] / abs(loss) if loss else None

    # status now
    if price is not None:
        if price <= stop:
            res["status"] = "🔴 Below stop"
        elif tps and price >= tps[0]:
            n = sum(price >= t for t in tps)
            res["status"] = f"✅ At/above TP{n}"
        elif lo and hi and lo * (0.99 if lo == hi else 0.995) <= price <= hi * (1.01 if lo == hi else 1.005):
            res["status"] = "🟢 In buy zone"
        elif hi and price > hi:
            res["status"] = f"⏫ {((price / hi) - 1) * 100:.1f}% above buy zone"
        else:
            res["status"] = "⏳ Waiting"

    if res["outcome"] in ("Stop hit",) or "stopped" in res["outcome"]:
        verdict = "❌ Skip"
        flags.append("Stop already hit since the call")
    if res.get("rr_last") is not None and res["rr_last"] < 1:
        verdict = "❌ Skip"
        flags.append("Even the final target pays less than the stop costs (after costs & tax)")
    if res.get("rr_tp1") is not None and res["rr_tp1"] < 1:
        flags.append("TP1 alone pays less than the stop: needs TP2+")
    if res["stop_pct"] > 5:
        flags.append(f"Wide stop ({res['stop_pct']:.1f}%): sized down to {shares:,} shares")
    if res.get("above_sma200") is False:
        flags.append("Below its 200-day average (downtrend)")
    if entry < 20:
        flags.append("Cheap share: per-share commission eats more")
    if res.get("avg_vol_20d") is not None and res["avg_vol_20d"] < 100_000:
        flags.append("Thin trading (<100k shares/day)")
    if "status" in res and "above buy zone" in res["status"]:
        pct = float(res["status"].split("%")[0].split()[-1])
        if pct > 2:
            flags.append("Already ran above the buy zone: don't chase")
    if verdict.startswith("✅") and len(flags) >= 2:
        verdict = "⚠️ Caution"
    res["verdict"], res["flags"] = verdict, flags
    return res


def track_record(evals: list[dict]) -> dict:
    done = [e["outcome"] for e in evals if e.get("outcome") not in (None, "No history", "Not filled yet", "Open")]
    wins = sum(o.startswith("TP") and "stopped" not in o for o in done)
    partial = sum("stopped" in o for o in done)
    return {"closed": len(done), "wins": wins, "partial": partial, "losses": len(done) - wins - partial}
