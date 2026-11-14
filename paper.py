"""Paper-trading log.

Records every buy setup the scanner flags in market hours and follows it with the
trading plan's rules, so the signals' real win rate after costs can be measured
before money goes in:
- fill at the setup's buy price, same share count the app suggests
- target hit -> sold at target
- stop hit -> sold at the stop (or the lower price seen, if it gapped through)
- up 1x risk -> stop moves to breakeven
- still open at the exit time (3:15 pm) -> sold at that price
One paper trade per stock per day; no new ones after the entry cut-off.

Saved to a local file and, when a GitHub token with gist scope is set, to a private
gist so it survives app restarts.
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

from signals import ACTIONABLE, Costs

log = logging.getLogger("psx-paper")
PAPER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.json")
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


def net_result(entry: float, exit_px: float, shares: int, costs: Costs) -> float:
    """Rupees after both sides' costs, and capital gains tax on a profit."""
    pre = (exit_px - entry) * shares - costs.per_side(shares, entry) - costs.per_side(shares, exit_px)
    return round(pre * (1 - costs.cgt) if pre > 0 else pre, 0)


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


def by_signal(trades: list[PaperTrade]) -> pd.DataFrame:
    rows = []
    for sig in sorted({t.signal for t in trades}):
        s = stats([t for t in trades if t.signal == sig])
        rows.append({"Signal": sig, "Closed": s["closed"],
                     "Win rate": f"{s['win_rate']:.0%}" if s["win_rate"] is not None else "–",
                     "Avg per trade (Rs)": f"{s['avg']:+,.0f}" if s["avg"] is not None else "–",
                     "Total (Rs)": f"{s['total']:+,.0f}"})
    return pd.DataFrame(rows)


def to_frame(trades: list[PaperTrade]) -> pd.DataFrame:
    rows = [{"Date": t.date, "Time": t.opened, "Symbol": t.symbol, "Signal": t.signal,
             "Buy": t.entry, "Stop": t.stop, "Target": t.target, "Shares": t.shares,
             "High seen": t.hi, "Low seen": t.lo, "Result": STATUS_LABEL.get(t.status, t.status),
             "Sold at": t.exit_px if t.exit_px is not None else t.last, "Sold time": t.exit_at,
             "Net (Rs)": t.net} for t in trades]
    return pd.DataFrame(rows)


def day_summary(trades: list[PaperTrade], day: str) -> str | None:
    today = [t for t in trades if t.date == day]
    if not today:
        return None
    s, alls = stats(today), stats(trades)
    lines = [f"📒 <b>Paper trades today: {s['closed']} closed, net Rs {s['total']:+,.0f}</b>",
             f"Target {s['target']} · Stop {s['stop']} · Breakeven {s['breakeven']} · Sold 3:15 {s['time']}"]
    for t in today:
        lines.append(f"{t.symbol} {STATUS_LABEL.get(t.status, t.status)}: Rs {t.net or 0:+,.0f}")
    if alls["win_rate"] is not None:
        lines.append(f"All time: {alls['closed']} trades, {alls['win_rate']:.0%} winners, "
                     f"avg Rs {alls['avg']:+,.0f} per trade")
    return "\n".join(lines)


class PaperBook:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.trades: list[PaperTrade] = self._load_file()
        self.token = None          # token the remote copy was loaded with
        self.gist_id = ""
        self.remote_error = ""
        self.remote_saved_at = 0.0
        self._remote_dirty = False

    # -- storage
    def _load_file(self) -> list[PaperTrade]:
        try:
            with open(PAPER_FILE) as f:
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
                mine = {t.id: t for t in self.trades}
                for t in remote:  # keep whichever copy has progressed further
                    m = mine.get(t.id)
                    if m is None or (m.status == "open" and t.status != "open"):
                        mine[t.id] = t
                self.trades = sorted(mine.values(), key=lambda t: (t.date, t.opened, t.symbol))
                self._remote_dirty = len(mine) != len(remote)
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
               entry_cutoff: dtime = ENTRY_CUTOFF, exit_time: dtime = EXIT_TIME) -> list[PaperTrade]:
        """Open new paper trades from the scan, move open ones on. Returns trades opened or closed now."""
        day, hm = now.date().isoformat(), now.strftime("%H:%M")
        events: list[PaperTrade] = []
        with self.lock:
            ids = {t.id for t in self.trades}
            # 1) follow open trades
            for t in self.trades:
                if t.status != "open":
                    continue
                if t.date != day:  # left open (app asleep at 3:15): sold at the last price seen
                    self._close(t, "time", t.last or t.entry, f"{t.date} last", costs)
                    events.append(t)
                    continue
                px = prices.get(t.symbol)
                if px is None or pd.isna(px):
                    continue
                px = float(px)
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
                    events.append(t)
            # 2) record new setups
            if scan is not None and not scan.empty and now.time() < entry_cutoff:
                for r in scan[scan["signal"].isin(ACTIONABLE)].itertuples():
                    tid = f"{day}-{r.symbol}"
                    if tid in ids or not r.shares or pd.isna(r.entry):
                        continue
                    px = float(r.current)
                    t = PaperTrade(id=tid, date=day, opened=hm, symbol=r.symbol, signal=r.signal,
                                   score=float(r.score), entry=float(r.entry), stop=float(r.stop),
                                   target=float(r.target), shares=int(r.shares),
                                   risk=round(float(r.entry) - float(r.stop), 4), stop_now=float(r.stop),
                                   hi=px, lo=px, last=px)
                    self.trades.append(t)
                    ids.add(tid)
                    events.append(t)
            changed = bool(events)
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
