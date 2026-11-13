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

import pandas as pd
import requests

from psx_client import PSXClient, PSXError
from signals import ACTIONABLE, Costs, Settings, build_scan, session_vwap

PKT = ZoneInfo("Asia/Karachi")
log = logging.getLogger("psx-alerts")
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")


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


NTFY_PRIORITY = {"stop": 5, "target": 4, "breakeven": 4, "close_soon": 4, "buy": 4}
NTFY_TAGS = {"stop": ["rotating_light"], "target": ["moneybag"], "breakeven": ["shield"],
             "close_soon": ["alarm_clock"], "buy": ["chart_with_upwards_trend"]}


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


def evaluate(scan: pd.DataFrame, prices: dict, positions: list[Position], sent: set,
             now: datetime, close_t: dtime, costs: Costs, buy_when_holding: bool = False,
             max_buy_alerts: int = 3) -> list[Alert]:
    """Pure rules: which alerts to send now. Mutates positions (breakeven stop move)."""
    alerts: list[Alert] = []

    # 1) exits for open positions
    for p in positions:
        px = prices.get(p.symbol)
        if px is None or pd.isna(px):
            continue
        pl = (px - p.entry) * p.shares - costs.per_side(p.shares, p.entry) - costs.per_side(p.shares, px)
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
            be = p.entry + 2 * costs.per_side(p.shares, p.entry) / p.shares
            p.stop, p.breakeven_moved = round(be, 2), True
            alerts.append(Alert(f"{p.symbol}-be", "breakeven", p.symbol,
                                f"🔵 <b>{p.symbol}: move stop-loss to {fmt(be)}</b> (breakeven)\n"
                                f"Price {fmt(px)} is up 1× your risk. Update the stop in KTrade.\n"
                                f"Target still {fmt(p.target)}."))
    close_dt = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
    if positions and close_dt - timedelta(minutes=15) <= now <= close_dt and "close-soon" not in sent:
        names = ", ".join(p.symbol for p in positions)
        alerts.append(Alert("close-soon", "close_soon", names,
                            f"⏰ <b>Market closes in ~15 min.</b> You still hold {names}.\n"
                            "Sell now, unless it's closing strong above VWAP and you plan to hold."))

    # 2) new buy setups
    if (not positions or buy_when_holding) and scan is not None and not scan.empty:
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


# ---------------------------------------------------------------- engine
class Engine:
    """Shared state + background loop. One per server process."""

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

    def vwaps(self) -> dict:
        with self.lock:
            return {s: v for s, sn in self.snaps.items() if (v := session_vwap(sn))}

    def session_fraction(self, now: datetime, open_t: dtime, close_t: dtime) -> float:
        if not market_open(now, open_t, close_t):
            return 1.0
        start = now.replace(hour=open_t.hour, minute=open_t.minute, second=0, microsecond=0)
        end = now.replace(hour=close_t.hour, minute=close_t.minute, second=0, microsecond=0)
        return (now - start) / (end - start)

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
        if not is_open:
            return []
        self.record(board, now.timestamp())
        settings: Settings = cfg["settings"]
        costs: Costs = cfg["costs"]
        scan = build_scan(board, self.avg_vol, self.kse100, self.vwaps(),
                          self.session_fraction(now, cfg["open_t"], cfg["close_t"]),
                          settings, costs, cfg["kse_only"])
        prices = dict(zip(board["symbol"], board["current"]))
        with self.lock:
            alerts = evaluate(scan, prices, self.positions, self.sent, now, cfg["close_t"], costs,
                              cfg.get("buy_when_holding", False))
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
