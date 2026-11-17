"""Paper-trading journal.

Records every buy setup the scanner flags in market hours and follows it with the
trading plan's rules, so the signals' real win rate after costs can be measured
before money goes in:
- fill at the setup's buy price, same share count the app suggests
- target hit -> sold at target
- stop hit -> sold at the stop (or the lower price seen, if it gapped through)
- up 1x risk -> stop moves to breakeven
- still open at the exit time (3:15 pm) -> sold at that price
One paper trade per stock per day; no new ones after the entry cut-off.

Each trade also stores the conditions when it was opened (time, move on the day,
volume, distance from VWAP, stop width, market mood) and how far it went for and
against us, so after a week or two the journal shows which conditions make money
and new rules can be based on data. Setups skipped as "Costs too high" are followed
the same way (marked as skipped, not counted in the headline numbers) to check
that the skip rule is right.

Saved to a local file and, when a GitHub token with gist scope is set, to a private
gist so it survives app restarts. Trades recorded before the gist backup existed are
kept in paper_seed.json and merged in on start.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, time as dtime

import pandas as pd
import requests

from signals import ACTIONABLE, COSTLY, RULES_VERSION, Costs

log = logging.getLogger("psx-paper")
HERE = os.path.dirname(os.path.abspath(__file__))
PAPER_FILE = os.path.join(HERE, "paper_trades.json")
SEED_FILE = os.path.join(HERE, "paper_seed.json")
GIST_DESC = "PSX scanner paper trades (used by the app, keep private)"
GIST_FILE = "paper_trades.json"
API = "https://api.github.com"
ENTRY_CUTOFF = dtime(15, 0)
EXIT_TIME = dtime(15, 15)
MIN_TRADES = 20  # fewer closed trades than this and the numbers mean little

STATUS_LABEL = {"open": "Open", "target": "Target", "stop": "Stop", "breakeven": "Breakeven",
                "time": "Sold 3:15"}


@dataclass
class PaperTrade:
    id: str
    date: str
    opened: str
    symbol: str
    signal: str
    score: float
    entry: float
    stop: float
    target: float
    shares: int
    risk: float
    stop_now: float
    be_moved: bool = False
    hi: float = 0.0
    lo: float = 0.0
    last: float = 0.0
    status: str = "open"
    exit_px: float | None = None
    exit_at: str = ""
    net: float | None = None
    # journal: what the rules and the market looked like when the trade was opened
    rules: int = 1                    # signals.RULES_VERSION at entry (1 = before 17 Nov 13:30)
    taken: bool = True                # False = setup the rules skipped (Costs too high), followed anyway
    chg_pct: float | None = None      # stock's % change on the day at entry
    rvol: float | None = None         # volume vs its normal pace for that time of day
    pos_range: float | None = None    # 0 = at the day's low, 1 = at the day's high
    vwap_gap: float | None = None     # % above (+) or below (-) VWAP at entry
    stop_pct: float | None = None     # stop distance, % of entry
    rr_cost: float | None = None      # profit at target / loss at stop, after costs
    mkt_chg: float | None = None      # average % change of KSE-100 stocks at entry (market mood)
    breadth: float | None = None      # share of all stocks that were up at entry
    hi_at: str = ""                   # time the best price was seen
    lo_at: str = ""                   # time the worst price was seen


def net_result(entry: float, exit_px: float, shares: int, costs: Costs) -> float:
    """Rupees after both sides' costs, and capital gains tax on a profit."""
    pre = (exit_px - entry) * shares - costs.per_side(shares, entry) - costs.per_side(shares, exit_px)
    return round(pre * (1 - costs.cgt) if pre > 0 else pre, 0)


def taken(trades: list[PaperTrade]) -> list[PaperTrade]:
    return [t for t in trades if t.taken]


def stats(trades: list[PaperTrade]) -> dict:
    closed = [t for t in trades if t.status != "open" and t.net is not None]
    nets = [t.net for t in closed]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    counts = {k: sum(t.status == k for t in closed) for k in ("target", "stop", "breakeven", "time")}
    return {
        "n": len(trades), "closed": len(closed), "open": len(trades) - len(closed),
        "wins": len(wins), "win_rate": len(wins) / len(closed) if closed else None,
        "total": sum(nets), "avg": sum(nets) / len(nets) if nets else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "best": max(nets) if nets else None, "worst": min(nets) if nets else None,
        "days": len({t.date for t in trades}), **counts,
    }


# ---------------------------------------------------------------- journal views
def _pct(a, b):
    return round((a / b - 1) * 100, 2) if a and b else None


def _mins_after_open(hm: str, open_t: dtime = dtime(9, 30)) -> int | None:
    try:
        h, m = map(int, hm.split(":")[:2])
    except (ValueError, AttributeError):
        return None
    return h * 60 + m - (open_t.hour * 60 + open_t.minute)


def entry_slot(t: PaperTrade) -> str:
    m = _mins_after_open(t.opened)
    if m is None:
        return "Unknown"
    return "09:30–11:00" if m < 90 else "11:00–12:30" if m < 180 else "After 12:30"


def market_mood(t: PaperTrade) -> str:
    if t.mkt_chg is None:
        return "Not recorded"
    return "Market up (>+0.3%)" if t.mkt_chg > 0.3 else "Market down (<−0.3%)" if t.mkt_chg < -0.3 else "Market flat"


def volume_band(t: PaperTrade) -> str:
    if t.rvol is None:
        return "Not recorded"
    return "Under 1.5×" if t.rvol < 1.5 else "1.5–3×" if t.rvol < 3 else "3× or more"


def stop_width(t: PaperTrade) -> float | None:
    if t.stop_pct is not None:
        return t.stop_pct
    return round((t.entry - t.stop) / t.entry * 100, 2) if t.entry else None


def stop_band(t: PaperTrade) -> str:
    sp = stop_width(t)
    if sp is None:
        return "Unknown"
    return "Under 1%" if sp < 1 else "1–1.6%" if sp < 1.6 else "1.6–2%"


GROUPS = {
    "Signal": lambda t: t.signal,
    "Entry time": entry_slot,
    "Market mood": market_mood,
    "Volume vs normal": volume_band,
    "Stop width": stop_band,
    "Rules version": lambda t: f"v{t.rules}",
    "Taken or skipped": lambda t: "Taken (alerted)" if t.taken else "Skipped (costs too high)",
}


def breakdown(trades: list[PaperTrade], by: str) -> pd.DataFrame:
    """Results grouped by one entry condition: which conditions make money."""
    key = GROUPS[by]
    rows = []
    for k in sorted({key(t) for t in trades}):
        grp = [t for t in trades if key(t) == k]
        s = stats(grp)
        closed = [t for t in grp if t.status != "open"]
        best = [_pct(t.hi, t.entry) for t in closed if t.hi]
        r1 = [t for t in closed if t.risk > 0 and t.hi >= t.entry + t.risk]
        rows.append({by: k, "Closed": s["closed"],
                     "Win rate": f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "–",
                     "Avg per trade (Rs)": f"{s['avg']:+,.0f}" if s["avg"] is not None else "–",
                     "Total (Rs)": f"{s['total']:+,.0f}",
                     "Avg best move": f"{sum(best) / len(best):+.2f}%" if best else "–",
                     "Reached +1× risk": f"{len(r1) / len(closed):.0%}" if closed else "–"})
    return pd.DataFrame(rows)


def to_frame(trades: list[PaperTrade]) -> pd.DataFrame:
    rows = [{"Date": t.date, "Time": t.opened, "Symbol": t.symbol, "Signal": t.signal,
             "Buy": t.entry, "Stop": t.stop, "Target": t.target, "Shares": t.shares,
             "High seen": t.hi, "Low seen": t.lo, "Result": STATUS_LABEL.get(t.status, t.status),
             "Sold at": t.exit_px if t.exit_px is not None else t.last, "Sold time": t.exit_at,
             "Net (Rs)": t.net} for t in trades]
    return pd.DataFrame(rows)


def journal_frame(trades: list[PaperTrade]) -> pd.DataFrame:
    """Every field, one row per trade (taken and skipped): the CSV to study and build rules from."""
    rows = []
    for t in trades:
        exit_px = t.exit_px if t.exit_px is not None else t.last
        rows.append({
            "date": t.date, "time": t.opened, "symbol": t.symbol, "signal": t.signal,
            "taken": "yes" if t.taken else "skipped", "rules": t.rules, "score": t.score,
            "entry_slot": entry_slot(t), "chg_pct_at_entry": t.chg_pct, "rvol": t.rvol,
            "pos_in_range": t.pos_range, "vwap_gap_pct": t.vwap_gap, "market_chg_pct": t.mkt_chg,
            "breadth_up_pct": round(t.breadth * 100, 1) if t.breadth is not None else None,
            "buy": t.entry, "stop": t.stop, "target": t.target, "shares": t.shares,
            "stop_pct": stop_width(t),
            "rr_after_costs": t.rr_cost,
            "best_move_pct": _pct(t.hi, t.entry), "best_at": t.hi_at,
            "worst_move_pct": _pct(t.lo, t.entry), "worst_at": t.lo_at,
            "reached_1r": bool(t.risk > 0 and t.hi >= t.entry + t.risk),
            "result": STATUS_LABEL.get(t.status, t.status), "sold_at": exit_px, "sold_time": t.exit_at,
            "exit_move_pct": _pct(exit_px, t.entry), "net_rs": t.net,
        })
    return pd.DataFrame(rows)


def day_summary(trades: list[PaperTrade], day: str) -> str | None:
    trades = taken(trades)
    today = [t for t in trades if t.date == day]
    if not today:
        return None
    s, alls = stats(today), stats(trades)
    lines = [f"📒 <b>Paper trades today: {s['closed']} closed, net Rs {s['total']:+,.0f}</b>",
             f"Target {s['target']} · Stop {s['stop']} · Breakeven {s['breakeven']} · Sold 3:15 {s['time']}"]
    for t in today:
        lines.append(f"{t.symbol} {STATUS_LABEL.get(t.status, t.status)}: Rs {t.net or 0:+,.0f}")
    if alls["win_rate"] is not None:
        lines.append(f"All time: {alls['closed']} trades over {alls['days']} days, {alls['win_rate']:.0%} winners, "
                     f"avg Rs {alls['avg']:+,.0f} per trade")
    return "\n".join(lines)


class PaperBook:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.trades: list[PaperTrade] = self._merge(self._load_file(PAPER_FILE), self._load_file(SEED_FILE))
        self.token = None          # token the remote copy was loaded with
        self.gist_id = ""
        self.remote_error = ""
        self.remote_saved_at = 0.0
        self._remote_dirty = False

    # -- storage
    def _load_file(self, path: str) -> list[PaperTrade]:
        try:
            with open(path) as f:
                return self._parse(json.load(f))
        except (OSError, ValueError):
            return []

    @staticmethod
    def _parse(rows) -> list[PaperTrade]:
        names = {f.name for f in fields(PaperTrade)}
        out = []
        for r in rows if isinstance(rows, list) else []:
            try:
                out.append(PaperTrade(**{k: v for k, v in r.items() if k in names}))
            except TypeError:
                continue
        return out

    @staticmethod
    def _merge(mine: list[PaperTrade], other: list[PaperTrade]) -> list[PaperTrade]:
        """Union by id, keeping whichever copy has progressed further."""
        by_id = {t.id: t for t in mine}
        for t in other:
            m = by_id.get(t.id)
            if m is None or (m.status == "open" and t.status != "open"):
                by_id[t.id] = t
        return sorted(by_id.values(), key=lambda t: (t.date, t.opened, t.symbol))

    def _dump(self) -> str:
        return json.dumps([asdict(t) for t in self.trades], indent=1)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"}

    def connect(self, token: str) -> None:
        """Load (or create) the private gist once per token and merge it with what's in memory."""
        if token == self.token:
            return
        self.token = token
        if not token:
            self.gist_id, self.remote_error = "", ""
            return
        try:
            page, found = 1, None
            while not found and page <= 5:
                r = requests.get(f"{API}/gists", headers=self._headers(),
                                 params={"per_page": 100, "page": page}, timeout=20)
                if r.status_code != 200:
                    raise RuntimeError(f"GitHub {r.status_code}: {r.text[:120]}")
                items = r.json()
                found = next((g for g in items if g.get("description") == GIST_DESC), None)
                if len(items) < 100:
                    break
                page += 1
            if found:
                g = requests.get(f"{API}/gists/{found['id']}", headers=self._headers(), timeout=20).json()
                remote = self._parse(json.loads(g["files"][GIST_FILE]["content"] or "[]"))
                self.gist_id = found["id"]
            else:
                r = requests.post(f"{API}/gists", headers=self._headers(), timeout=20, json={
                    "description": GIST_DESC, "public": False,
                    "files": {GIST_FILE: {"content": "[]"}}})
                if r.status_code != 201:
                    raise RuntimeError(f"GitHub {r.status_code}: {r.text[:120]}")
                remote, self.gist_id = [], r.json()["id"]
            with self.lock:
                self.trades = self._merge(self.trades, remote)
                self._remote_dirty = len(self.trades) != len(remote)
            self.remote_error = ""
            self.save(force_remote=self._remote_dirty)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
            self.gist_id, self.remote_error = "", f"Backup not available: {e}"
            log.warning("paper gist: %s", e)

    def save(self, force_remote: bool = False) -> None:
        with self.lock:
            text = self._dump()
        try:
            with open(PAPER_FILE, "w") as f:
                f.write(text)
        except OSError:
            pass
        if not (self.token and self.gist_id):
            return
        if not force_remote and not (self._remote_dirty and time.time() - self.remote_saved_at > 600):
            return
        try:
            r = requests.patch(f"{API}/gists/{self.gist_id}", headers=self._headers(), timeout=20,
                               json={"files": {GIST_FILE: {"content": text}}})
            if r.status_code == 200:
                self._remote_dirty, self.remote_saved_at, self.remote_error = False, time.time(), ""
            else:
                self.remote_error = f"Backup failed: GitHub {r.status_code}"
        except requests.RequestException as e:
            self.remote_error = f"Backup failed: {e.__class__.__name__}"

    # -- rules
    def update(self, scan: pd.DataFrame | None, prices: dict, now: datetime, costs: Costs,
               entry_cutoff: dtime = ENTRY_CUTOFF, exit_time: dtime = EXIT_TIME,
               market: dict | None = None) -> list[PaperTrade]:
        """Open new paper trades from the scan, move open ones on. Returns taken trades opened or
        closed now (skipped setups are followed silently). `market` = {"mkt_chg", "breadth"}."""
        day, hm = now.date().isoformat(), now.strftime("%H:%M")
        market = market or {}
        events: list[PaperTrade] = []
        changed = False
        with self.lock:
            ids = {t.id for t in self.trades}
            # 1) follow open trades
            for t in self.trades:
                if t.status != "open":
                    continue
                if t.date != day:  # left open (app asleep at 3:15): sold at the last price seen
                    self._close(t, "time", t.last or t.entry, f"{t.date} last", costs)
                    changed = True
                    if t.taken:
                        events.append(t)
                    continue
                px = prices.get(t.symbol)
                if px is None or pd.isna(px):
                    continue
                px = float(px)
                if px > t.hi:
                    t.hi_at = hm
                if px < t.lo:
                    t.lo_at = hm
                t.last, t.hi, t.lo = px, max(t.hi, px), min(t.lo, px)
                self._remote_dirty = True  # pushed at most every 10 min while trades are open
                if px <= t.stop_now:
                    self._close(t, "breakeven" if t.be_moved else "stop", min(px, t.stop_now), hm, costs)
                elif px >= t.target:
                    self._close(t, "target", t.target, hm, costs)
                elif now.time() >= exit_time:
                    self._close(t, "time", px, hm, costs)
                elif not t.be_moved and t.risk > 0 and px >= t.entry + t.risk:
                    be = t.entry + 2 * costs.per_side(t.shares, t.entry) / max(t.shares, 1)
                    t.stop_now, t.be_moved = round(be, 2), True
                if t.status != "open":
                    changed = True
                    if t.taken:
                        events.append(t)
            # 2) record new setups (alerted ones, and skipped ones to check the skip rule)
            if scan is not None and not scan.empty and now.time() < entry_cutoff:
                for r in scan[scan["signal"].isin(ACTIONABLE + (COSTLY,))].itertuples():
                    is_taken = r.signal in ACTIONABLE
                    tid = f"{day}-{r.symbol}" + ("" if is_taken else "-skip")
                    if tid in ids or pd.isna(r.shares) or not r.shares or pd.isna(r.entry):
                        continue
                    px, entry = float(r.current), float(r.entry)
                    vwap = getattr(r, "vwap", None)
                    t = PaperTrade(
                        id=tid, date=day, opened=hm, symbol=r.symbol, signal=r.signal,
                        score=float(r.score), entry=entry, stop=float(r.stop), target=float(r.target),
                        shares=int(r.shares), risk=round(entry - float(r.stop), 4), stop_now=float(r.stop),
                        hi=px, lo=px, last=px, hi_at=hm, lo_at=hm,
                        rules=RULES_VERSION, taken=is_taken,
                        chg_pct=_num(r.change_pct, 2), rvol=_num(r.rvol, 2), pos_range=_num(r.pos_in_range, 2),
                        vwap_gap=_pct(px, float(vwap)) if vwap is not None and not pd.isna(vwap) else None,
                        stop_pct=round((entry - float(r.stop)) / entry * 100, 2),
                        rr_cost=_num(getattr(r, "rr_after_costs", None), 2),
                        mkt_chg=_num(market.get("mkt_chg"), 2), breadth=_num(market.get("breadth"), 3))
                    self.trades.append(t)
                    ids.add(tid)
                    changed = True
                    if is_taken:
                        events.append(t)
            if changed:
                self._remote_dirty = True
        if changed or any(t.status == "open" for t in self.trades):
            self.save(force_remote=changed)
        return events

    @staticmethod
    def _close(t: PaperTrade, status: str, px: float, at: str, costs: Costs) -> None:
        t.status, t.exit_px, t.exit_at = status, round(px, 2), at
        t.net = net_result(t.entry, t.exit_px, t.shares, costs)

    def snapshot(self) -> list[PaperTrade]:
        with self.lock:
            return list(self.trades)


def _num(v, d: int):
    try:
        return None if v is None or pd.isna(v) else round(float(v), d)
    except (TypeError, ValueError):
        return None
