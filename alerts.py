"""Background alert engine: scans PSX every minute and sends Telegram alerts.

Runs in a background thread inside the Streamlit server process, so it keeps
working when no browser has the page open (as long as the app is awake).
It never calls Streamlit functions.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from broker_calls import RISK_BUDGET, evaluate as evaluate_call, load_calls
from psx_client import PSXClient, PSXError
from paper import PaperBook, day_summary
import swing
from signals import ACTIONABLE, VWAP_WARMUP_MIN, Costs, Settings, build_scan, session_vwap

PKT = ZoneInfo("Asia/Karachi")
log = logging.getLogger("psx-alerts")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")
PASTED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pasted_calls.json")


# ---------------------------------------------------------------- telegram
def send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "Telegram not set up"
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        return ok, "sent" if ok else f"Telegram error {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return False, f"Telegram unreachable: {e.__class__.__name__}"


def to_whatsapp(text: str) -> str:
    return text.replace("<b>", "*").replace("</b>", "*")


def send_whatsapp(phone: str, apikey: str, text: str) -> tuple[bool, str]:
    """Free personal WhatsApp gateway (CallMeBot): messages go only to your own number."""
    if not phone or not apikey:
        return False, "WhatsApp not set up"
    try:
        r = requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": phone, "text": to_whatsapp(text), "apikey": apikey}, timeout=25)
        body = r.text.lower()[:500]
        ok = r.status_code == 200 and "error" not in body and "invalid" not in body
        return ok, "sent" if ok else f"WhatsApp error {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return False, f"WhatsApp unreachable: {e.__class__.__name__}"


NTFY_PRIORITY = {"stop": 5, "target": 4, "breakeven": 4, "close_soon": 4, "buy": 4,
                 "bc_new": 4, "bc_zone": 4, "bc_tp": 3, "bc_stop": 4, "paper": 2}
NTFY_TAGS = {"stop": ["rotating_light"], "target": ["moneybag"], "breakeven": ["shield"],
             "close_soon": ["alarm_clock"], "buy": ["chart_with_upwards_trend"],
             "bc_new": ["loudspeaker"], "bc_zone": ["dart"], "bc_tp": ["moneybag"], "bc_stop": ["warning"],
             "paper": ["ledger"]}


def send_ntfy(topic: str, text: str, kind: str = "test") -> tuple[bool, str]:
    """Push notification to the free ntfy app (ntfy.sh). Stop-loss alerts go out as urgent."""
    if not topic:
        return False, "ntfy not set up"
    plain = text.replace("<b>", "").replace("</b>", "")
    title, _, body = plain.partition("\n")
    try:
        r = requests.post("https://ntfy.sh/", json={
            "topic": topic, "title": title, "message": body or title,
            "priority": NTFY_PRIORITY.get(kind, 3), "tags": NTFY_TAGS.get(kind, [])}, timeout=15)
        ok = r.status_code == 200
        return ok, "sent" if ok else f"ntfy error {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        return False, f"ntfy unreachable: {e.__class__.__name__}"


def notify(cfg: dict, text: str, kind: str = "info") -> list[str]:
    """Send to every channel that is set up. Returns problems (empty list = all sent)."""
    problems = []
    if cfg.get("ntfy_topic"):
        ok, msg = send_ntfy(cfg["ntfy_topic"], text, kind)
        if not ok:
            problems.append(msg)
    if cfg.get("tg_token") and cfg.get("tg_chat"):
        ok, msg = send_telegram(cfg["tg_token"], cfg["tg_chat"], text)
        if not ok:
            problems.append(msg)
    if cfg.get("wa_phone") and cfg.get("wa_key"):
        ok, msg = send_whatsapp(cfg["wa_phone"], cfg["wa_key"], text)
        if not ok:
            problems.append(msg)
    return problems


def find_chat_ids(token: str) -> list[tuple[str, str]]:
    """Chats that recently messaged the bot: [(chat_id, name)]."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    out = {}
    for u in r.json().get("result", []):
        chat = (u.get("message") or u.get("edited_message") or {}).get("chat")
        if chat:
            out[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
    return list(out.items())


# ---------------------------------------------------------------- model
@dataclass
class Position:
    symbol: str
    entry: float
    shares: int
    stop: float
    target: float
    opened: str = ""
    breakeven_moved: bool = False
    kind: str = "day"         # "day" = sell by the close; "swing" = hold up to SwingSettings.hold_days

    @property
    def risk_per_share(self) -> float:
        return self.entry - self.stop


@dataclass
class Alert:
    key: str          # de-duplication key (one alert per key per day)
    kind: str         # buy | stop | target | breakeven | close_soon
    symbol: str
    text: str
    at: str = field(default_factory=lambda: datetime.now(PKT).strftime("%H:%M:%S"))


def market_open(now: datetime, open_t: dtime, close_t: dtime) -> bool:
    return now.weekday() < 5 and open_t <= now.time() <= close_t


def fmt(x: float) -> str:
    return f"{x:,.2f}"


def held_days(p: Position, now: datetime) -> int:
    """Trading days (Mon-Fri) since the position was opened (0 = bought today)."""
    try:
        d0 = datetime.strptime(p.opened[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 0
    return max(int(np.busday_count(d0, now.date())), 0)


def position_pl(p: Position, px: float, costs: Costs, now: datetime | None = None) -> float:
    """Profit/loss after costs if sold now (same-day sells pay no commission at KTrade)."""
    same_day = now is not None and p.opened[:10] == now.strftime("%Y-%m-%d")
    return (px - p.entry) * p.shares - costs.per_side(p.shares, p.entry) - costs.sell_side(p.shares, px, same_day)


def evaluate(scan: pd.DataFrame, prices: dict, positions: list[Position], sent: set,
             now: datetime, close_t: dtime, costs: Costs, buy_when_holding: bool = False,
             max_buy_alerts: int = 3, buy_alerts: bool = True, hold_days: int = 4) -> list[Alert]:
    """Pure rules: which alerts to send now. Mutates positions (breakeven stop move)."""
    alerts: list[Alert] = []

    # 1) exits for open positions
    for p in positions:
        px = prices.get(p.symbol)
        if px is None or pd.isna(px):
            continue
        pl = position_pl(p, px, costs, now)
        if px <= p.stop and f"{p.symbol}-stop" not in sent:
            alerts.append(Alert(f"{p.symbol}-stop", "stop", p.symbol,
                                f"🔴 <b>SELL {p.symbol} now: stop-loss hit</b>\n"
                                f"Price {fmt(px)} ≤ stop {fmt(p.stop)}\n"
                                f"{p.shares:,} shares · P/L ≈ Rs {pl:,.0f} after costs\n"
                                f"Take the small loss. Don't hope."))
        elif px >= p.target and f"{p.symbol}-target" not in sent:
            alerts.append(Alert(f"{p.symbol}-target", "target", p.symbol,
                                f"✅ <b>SELL {p.symbol}: target reached</b>\n"
                                f"Price {fmt(px)} ≥ target {fmt(p.target)}\n"
                                f"{p.shares:,} shares · P/L ≈ Rs {pl:,.0f} before tax\n"
                                f"Book the profit."))
        elif (not p.breakeven_moved and p.risk_per_share > 0
              and px >= p.entry + p.risk_per_share and f"{p.symbol}-be" not in sent):
            same_day = p.opened[:10] == now.strftime("%Y-%m-%d")
            be = p.entry + (costs.per_side(p.shares, p.entry) + costs.sell_side(p.shares, p.entry, same_day)) / p.shares
            p.stop, p.breakeven_moved = round(be, 2), True
            alerts.append(Alert(f"{p.symbol}-be", "breakeven", p.symbol,
                                f"🔵 <b>{p.symbol}: move stop-loss to {fmt(be)}</b> (breakeven)\n"
                                f"Price {fmt(px)} is up 1× your risk. Update the stop in KTrade.\n"
                                f"Target still {fmt(p.target)}."))
    close_dt = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
    closing = close_dt - timedelta(minutes=15) <= now <= close_dt
    day_pos = [p for p in positions if p.kind != "swing"]
    if day_pos and closing and "close-soon" not in sent:
        names = ", ".join(p.symbol for p in day_pos)
        alerts.append(Alert("close-soon", "close_soon", names,
                            f"⏰ <b>Market closes in ~15 min.</b> You still hold {names}.\n"
                            "Sell now: intraday trades are sold the same day (no sell commission today)."))
    for p in positions:
        if p.kind == "swing" and closing and held_days(p, now) >= hold_days and f"{p.symbol}-timestop" not in sent:
            alerts.append(Alert(f"{p.symbol}-timestop", "close_soon", p.symbol,
                                f"⏰ <b>{p.symbol}: day {held_days(p, now)} of your swing trade.</b>\n"
                                "Neither stop nor target was hit: sell before the close (time stop)."))

    # 2) new buy setups
    if buy_alerts and (not positions or buy_when_holding) and scan is not None and not scan.empty:
        held = {p.symbol for p in positions}
        for r in scan[scan["signal"].isin(ACTIONABLE)].head(10).itertuples():
            if len([a for a in alerts if a.kind == "buy"]) >= max_buy_alerts:
                break
            key = f"{r.symbol}-buy"
            if key in sent or r.symbol in held:
                continue
            alerts.append(Alert(key, "buy", r.symbol,
                                f"🟢 <b>BUY setup: {r.symbol}</b> ({r.signal})\n"
                                f"Now {fmt(r.current)} ({r.change_pct:+.2f}%) · volume {r.rvol:.1f}× normal\n"
                                f"Buy ≈ {fmt(r.entry)} · Stop {fmt(r.stop)} · Target {fmt(r.target)}\n"
                                f"{int(r.shares):,} shares (Rs {r.position_rs:,.0f})\n"
                                f"At target: Rs {r.net_at_target:+,.0f} · At stop: Rs {r.loss_at_stop:,.0f}\n"
                                f"Confirm the price in KTrade first."))
    return alerts


def market_mood(board: pd.DataFrame, kse100: set) -> dict:
    """Market conditions for the journal: average % change of KSE-100 stocks, and the share of
    all stocks that are up."""
    out: dict = {}
    if board is None or board.empty or not {"current", "ldcp"} <= set(board.columns):
        return out
    chg = (board["current"] - board["ldcp"]) / board["ldcp"] * 100
    chg = chg.replace([float("inf"), float("-inf")], float("nan"))
    idx = chg[board["symbol"].isin(kse100)].dropna() if kse100 else chg.dropna()
    if not idx.empty:
        out["mkt_chg"] = float(idx.clip(-10, 10).mean())
    moved = chg.dropna()
    moved = moved[moved != 0]
    if not moved.empty:
        out["breadth"] = float((moved > 0).mean())
    return out


# ---------------------------------------------------------------- engine
class Engine:
    """Shared state + background loop. One per server process."""

    VERSION = 7  # bump when Engine changes, so a running app swaps in the new one

    def __init__(self, client: PSXClient | None = None, interval: int = 60) -> None:
        self.client = client or PSXClient()
        self.interval = interval
        self.lock = threading.RLock()
        self.config: dict | None = None
        self.board: pd.DataFrame | None = None
        self.board_at: float = 0.0
        self.board_error: str = ""
        self.snaps: dict = {}
        self.day = None
        self.sent: set = set()
        self.log: list[Alert] = []
        self.avg_vol: dict = {}
        self.kse100: set = set()
        self._ref_at = 0.0
        self.positions: list[Position] = self._load_positions()
        self._thread: threading.Thread | None = None
        self.last_tick = ""
        # broker calls
        self.calls: list[dict] = []
        self.calls_updated = ""
        self.calls_error = ""
        self.calls_loaded_at = 0.0
        self.pasted: list[dict] = self._load_pasted()
        self.known_calls: set | None = None
        self.hist_cache: dict = {}
        # paper-trading log
        self.paper = PaperBook()
        # swing (satellite) strategy: daily history for KSE-100 stocks, loaded once a day
        self.hist: dict = {}                  # symbol -> daily OHLCV
        self.index_hist: pd.DataFrame | None = None
        self.index_weights: pd.DataFrame | None = None
        self.hist_day = None                  # date the history was loaded
        self.hist_loaded_at: datetime | None = None
        self.hist_status = ""
        self.hist_progress = (0, 0)
        self._hist_thread: threading.Thread | None = None
        self.swing_live: pd.DataFrame | None = None
        self.swing_market_ok: bool | None = None
        self.swing_at = 0.0
        self.swing_trades: pd.DataFrame | None = None   # backtest trades for the default rules (cached daily)

    # -- persistence (best effort; Streamlit Cloud wipes files on redeploy)
    def _load_positions(self) -> list[Position]:
        try:
            with open(POSITIONS_FILE) as f:
                return [Position(**p) for p in json.load(f)]
        except (OSError, ValueError, TypeError):
            return []

    def save_positions(self) -> None:
        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump([asdict(p) for p in self.positions], f)
        except OSError:
            pass

    def add_position(self, p: Position) -> None:
        with self.lock:
            self.positions = [x for x in self.positions if x.symbol != p.symbol] + [p]
            for k in ("stop", "target", "be"):
                self.sent.discard(f"{p.symbol}-{k}")
            self.save_positions()

    def close_position(self, symbol: str) -> None:
        with self.lock:
            self.positions = [x for x in self.positions if x.symbol != symbol]
            self.save_positions()

    # -- broker calls
    def _load_pasted(self) -> list[dict]:
        try:
            with open(PASTED_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def add_pasted(self, call: dict) -> None:
        with self.lock:
            self.pasted = [c for c in self.pasted if c["id"] != call["id"]] + [call]
            try:
                with open(PASTED_FILE, "w") as f:
                    json.dump(self.pasted, f)
            except OSError:
                pass

    def remove_pasted(self, call_id: str) -> None:
        with self.lock:
            self.pasted = [c for c in self.pasted if c["id"] != call_id]
            try:
                with open(PASTED_FILE, "w") as f:
                    json.dump(self.pasted, f)
            except OSError:
                pass

    def all_calls(self) -> list[dict]:
        ids = {c.get("id") for c in self.calls}
        return list(self.calls) + [c for c in self.pasted if c.get("id") not in ids]

    def refresh_calls(self, url: str, force: bool = False) -> None:
        if not url or (not force and time.time() - self.calls_loaded_at < 600):
            return
        try:
            calls, updated = load_calls(url)
            with self.lock:
                self.calls, self.calls_updated, self.calls_error = calls, updated, ""
        except Exception as e:  # noqa: BLE001 - show the problem, keep old data
            with self.lock:
                self.calls_error = f"{e.__class__.__name__}: {str(e)[:150]}"
        self.calls_loaded_at = time.time()

    def history(self, symbol: str, force: bool = False):
        hit = self.hist_cache.get(symbol)
        if hit and not force and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            df = self.client.history(symbol)
        except PSXError:
            df = hit[1] if hit else None
        self.hist_cache[symbol] = (time.time(), df)
        return df

    def broker_alerts(self, prices: dict, costs: Costs, now: datetime) -> list[Alert]:
        alerts: list[Alert] = []
        calls = [c for c in self.all_calls()
                 if c.get("date") and (now.date() - pd.Timestamp(c["date"]).date()).days <= 30]
        if self.known_calls is None:  # first load after a restart: don't re-announce
            self.known_calls = {c["id"] for c in calls}
        for c in calls:
            sym, cid = c["symbol"], c["id"]
            px = prices.get(sym)
            if cid not in self.known_calls:
                self.known_calls.add(cid)
                ev = evaluate_call(c, px, self.history(sym), costs, RISK_BUDGET)
                tps = "/".join(f"{t:g}" for t in (c.get("targets") or [])[:3])
                alerts.append(Alert(f"bc-{cid}-new", "bc_new", sym,
                                    f"📣 <b>New broker call: {sym}</b> ({c.get('source', '')})\n"
                                    f"Buy {c.get('entry_low')}–{c.get('entry_high')} · SL {c.get('stop')} · TP {tps}\n"
                                    f"Sense check: {ev.get('verdict', '')}"
                                    + (f" · size {ev['shares']:,} shares" if ev.get('shares') else "")
                                    + (f"\n{c['claude_note']}" if c.get("claude_note") else "")))
            if px is None or pd.isna(px):
                continue
            lo, hi = c.get("entry_low"), c.get("entry_high") or c.get("entry_low")
            stop, tps = c.get("stop"), c.get("targets") or []
            if c.get("or_below") and stop:
                lo = stop * 1.001
            if stop and px <= stop and f"bc-{cid}-stop" not in self.sent:
                alerts.append(Alert(f"bc-{cid}-stop", "bc_stop", sym,
                                    f"⚠️ <b>{sym}: broker's stop-loss {stop:g} hit</b> (now {px:,.2f})\n"
                                    "If you hold it from this call, exit."))
            elif tps and px >= tps[0] and f"bc-{cid}-tp1" not in self.sent:
                alerts.append(Alert(f"bc-{cid}-tp1", "bc_tp", sym,
                                    f"💰 <b>{sym} reached broker TP1 {tps[0]:g}</b> (now {px:,.2f})\n"
                                    "Book partial profit and move the stop to entry."))
            elif (lo and hi and lo * (0.99 if lo == hi else 0.995) <= px <= hi * (1.01 if lo == hi else 1.005)
                  and f"bc-{cid}-zone" not in self.sent):
                alerts.append(Alert(f"bc-{cid}-zone", "bc_zone", sym,
                                    f"🎯 <b>{sym} is in the broker's buy zone</b> {lo:g}–{hi:g} (now {px:,.2f})\n"
                                    "Check the sense check on the site before buying."))
        return alerts

    # -- data
    def refresh_reference(self, force: bool = False) -> None:
        if not force and time.time() - self._ref_at < 6 * 3600 and self.avg_vol:
            return
        try:
            sc = self.client.screener()
            if {"symbol", "volume_avg_30d"} <= set(sc.columns):
                self.avg_vol = dict(zip(sc["symbol"], sc["volume_avg_30d"]))
        except PSXError as e:
            log.warning("screener: %s", e)
        try:
            self.kse100 = set(self.client.index_members("KSE100")) or self.kse100
        except PSXError as e:
            log.warning("kse100: %s", e)
        self._ref_at = time.time()

    def fetch_board(self) -> pd.DataFrame | None:
        try:
            b = self.client.market_watch()
            with self.lock:
                self.board, self.board_at, self.board_error = b, time.time(), ""
            return b
        except PSXError as e:
            with self.lock:
                self.board_error = str(e)
            return None

    def record(self, board: pd.DataFrame, now_ts: float) -> None:
        vols = board["volume"] if "volume" in board.columns else pd.Series(0, index=board.index)
        with self.lock:
            for sym, px, vol in zip(board["symbol"], board["current"], vols):
                if pd.isna(px) or pd.isna(vol):
                    continue
                s = self.snaps.setdefault(sym, [])
                if not s or vol != s[-1][2]:
                    s.append((now_ts, float(px), float(vol)))

    def vwaps(self, warmup_min: int = VWAP_WARMUP_MIN) -> dict:
        """VWAP per stock, only once the app has watched it long enough to be meaningful.
        (After a restart or wake-up mid-session, VWAP starts from scratch and sits right on
        the price, which makes everything look like a VWAP bounce.)"""
        with self.lock:
            return {s: v for s, sn in self.snaps.items()
                    if sn and sn[-1][0] - sn[0][0] >= warmup_min * 60 and (v := session_vwap(sn))}

    @staticmethod
    def mins_open(now: datetime, open_t: dtime) -> float:
        start = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        return (now - start).total_seconds() / 60

    def session_fraction(self, now: datetime, open_t: dtime, close_t: dtime) -> float:
        if not market_open(now, open_t, close_t):
            return 1.0
        start = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        end = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        return (now - start) / (end - start)

    # -- swing data
    def history_ready(self) -> bool:
        return bool(self.hist) and self.index_hist is not None and not self.index_hist.empty

    def ensure_history(self, now: datetime | None = None, force: bool = False) -> None:
        """Load ~4 years of daily prices for the KSE-100 stocks in a background thread, once a day
        (after the close, or at the first use of the day)."""
        now = now or datetime.now(PKT)
        if self._hist_thread is not None and self._hist_thread.is_alive():
            return
        at = self.hist_loaded_at
        after_close = dtime(15, 45)
        fresh = at is not None and at.date() == now.date() and (at.time() >= after_close or now.time() < after_close)
        if not force and self.history_ready() and fresh:
            return
        self._hist_thread = threading.Thread(target=self._load_history, args=(now,), name="psx-history", daemon=True)
        self._hist_thread.start()

    def _load_history(self, now: datetime) -> None:
        try:
            self.refresh_reference()
            try:
                w = self.client.index_weights("KSE100")
                if not w.empty:
                    self.index_weights = w
            except PSXError as e:
                log.warning("index weights: %s", e)
            members = sorted(self.kse100) or (list(self.index_weights["symbol"]) if self.index_weights is not None else [])
            cutoff = pd.Timestamp(now.date()) - pd.DateOffset(years=4)
            self.hist_status = "loading"
            idx = self.client.history("KSE100")
            # before the close today's row is still moving: keep completed days only
            last_day = pd.Timestamp(now.date()) if now.time() >= dtime(15, 45) else pd.Timestamp(now.date()) - pd.Timedelta(days=1)
            idx = idx[idx["date"] <= last_day]
            out, fails = {}, []
            self.hist_progress = (0, len(members))
            for i, sym in enumerate(members, 1):
                try:
                    h = self.client.history(sym)
                    out[sym] = h[(h["date"] >= cutoff) & (h["date"] <= last_day)].reset_index(drop=True)
                except PSXError:
                    fails.append(sym)
                self.hist_progress = (i, len(members))
            with self.lock:
                self.hist, self.index_hist = out, idx[idx["date"] >= cutoff].reset_index(drop=True)
                self.hist_day, self.hist_loaded_at = now.date(), now
                self.swing_trades = None
            self.hist_status = "ready" + (f" ({len(fails)} stocks missing)" if fails else "")
        except Exception as e:  # never kill the app over history
            log.exception("history load")
            self.hist_status = f"error: {e}"

    def swing_settings(self) -> swing.SwingSettings:
        cfg = self.config or {}
        return cfg.get("swing") or swing.SwingSettings()

    def backtest_trades(self, costs: Costs | None = None) -> pd.DataFrame | None:
        """All historical swing setups for the current rules, followed to their exits (cached)."""
        if not self.history_ready():
            return None
        cfg = self.swing_settings()
        key = (tuple(sorted(cfg.__dict__.items())), self.hist_loaded_at)
        if self.swing_trades is None or getattr(self, "_swing_key", None) != key:
            self.swing_trades = swing.prepare(self.hist, self.index_hist, cfg, costs or Costs())
            self._swing_key = key
        return self.swing_trades

    def swing_step(self, board: pd.DataFrame, now: datetime, cfg: dict, costs: Costs) -> list[Alert]:
        """Near the close, look for swing setups using today's live prices; alert the best ones."""
        if not self.history_ready():
            return []
        close_dt = now.replace(hour=cfg["close_t"].hour, minute=cfg["close_t"].minute, second=0, microsecond=0)
        if not (close_dt - timedelta(minutes=35) <= now <= close_dt - timedelta(minutes=3)):
            return []
        if time.time() - self.swing_at < 240 and self.swing_live is not None:
            return []
        try:
            cand, ok = swing.live_candidates(self.hist, board, self.index_hist, None, now.date(),
                                             self.swing_settings(), costs)
        except Exception:
            log.exception("swing live")
            return []
        with self.lock:
            self.swing_live, self.swing_market_ok, self.swing_at = cand, ok, time.time()
        if not cfg.get("swing_alerts", True) or not ok or cand is None or cand.empty:
            return []
        if any(p.kind == "swing" for p in self.positions) and not cfg.get("buy_when_holding"):
            return []
        alerts = []
        for r in cand.head(2).itertuples():
            key = f"{r.symbol}-swing"
            if key in self.sent:
                continue
            alerts.append(Alert(key, "buy", r.symbol,
                                f"📈 <b>Swing {r.setup}: {r.symbol}</b> (satellite)\n"
                                f"Buy ≈ {fmt(r.entry)} before the close · Stop {fmt(r.stop)} ({r.stop_pct:.1f}%) · "
                                f"Target {fmt(r.target)}\n{int(r.shares):,} shares · At target Rs {r.net_at_target:+,.0f} "
                                f"· At stop Rs {r.loss_at_stop:,.0f}\nHold up to {self.swing_settings().hold_days} days. "
                                "Unproven rules: small money only."))
        return alerts

    # -- loop
    def tick(self, now: datetime | None = None) -> list[Alert]:
        now = now or datetime.now(PKT)
        cfg = self.config
        if cfg is None:
            return []
        with self.lock:
            if self.day != now.date():
                self.day, self.snaps, self.sent, self.log = now.date(), {}, set(), []
        is_open = market_open(now, cfg["open_t"], cfg["close_t"])
        if not is_open and self.board is not None and time.time() - self.board_at < 600:
            return []  # closed: refresh the display board every 10 min only
        board = self.fetch_board()
        if board is None:
            return []
        self.refresh_reference()
        self.ensure_history(now)
        if not is_open:
            return []
        self.record(board, now.timestamp())
        settings: Settings = cfg["settings"]
        costs: Costs = cfg["costs"]
        scan = build_scan(board, self.avg_vol, self.kse100, self.vwaps(),
                          self.session_fraction(now, cfg["open_t"], cfg["close_t"]),
                          settings, costs, cfg["kse_only"], mins_open=self.mins_open(now, cfg["open_t"]))
        prices = dict(zip(board["symbol"], board["current"]))
        self.refresh_calls(cfg.get("calls_url", ""))
        broker = self.broker_alerts(prices, costs, now)
        broker += self.paper_step(scan, prices, costs, now, cfg, market_mood(board, self.kse100))
        broker += self.swing_step(board, now, cfg, costs)
        with self.lock:
            alerts = evaluate(scan, prices, self.positions, self.sent, now, cfg["close_t"], costs,
                              cfg.get("buy_when_holding", False), buy_alerts=cfg.get("intraday_alerts", False),
                              hold_days=self.swing_settings().hold_days) + broker
            if any(a.kind == "breakeven" for a in alerts):
                self.save_positions()
        for a in alerts:
            for problem in notify(cfg, a.text, a.kind):
                log.warning("alert %s: %s", a.key, problem)
            with self.lock:
                self.sent.add(a.key)
                self.log.append(a)
                self.log = self.log[-50:]
        self.last_tick = now.strftime("%H:%M:%S")
        return alerts

    def paper_step(self, scan, prices: dict, costs: Costs, now: datetime, cfg: dict,
                   market: dict | None = None) -> list[Alert]:
        """Move the paper-trading log on; one summary push near the close."""
        close_dt = now.replace(hour=cfg["close_t"].hour, minute=cfg["close_t"].minute, second=0, microsecond=0)
        try:
            self.paper.connect(cfg.get("gh_token", ""))
            self.paper.update(scan, prices, now, costs,
                              entry_cutoff=(close_dt - timedelta(minutes=30)).time(),
                              exit_time=(close_dt - timedelta(minutes=15)).time(), market=market)
        except Exception:  # the log must never stop the alerts
            log.exception("paper trades")
            return []
        if now >= close_dt - timedelta(minutes=10) and "paper-summary" not in self.sent:
            text = day_summary(self.paper.snapshot(), now.date().isoformat())
            if text:
                return [Alert("paper-summary", "paper", "", text)]
        return []

    def _run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # never let the loop die
                log.exception("alert tick failed")
            time.sleep(self.interval)

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="psx-alerts", daemon=True)
            self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
