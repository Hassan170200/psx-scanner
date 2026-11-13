"""Minimal client for the public PSX Data Portal (dps.psx.com.pk).

PSX publishes no official API. This reads the same pages the portal's own
website uses. Since Sept 2026 PSX requires an `X-Req-Id` header on data
requests; its value is embedded in every portal HTML page as window.__ps._k.
If PSX changes its site, this file is the only one that needs fixing.
"""
from __future__ import annotations

import json
import re
import threading
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://dps.psx.com.pk"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": BASE + "/",
}
TIMEOUT = 20
TOKEN_TTL = 240  # PSX rotates the token roughly every 5 minutes

# Raw PSX header text (upper-cased) -> our column name
COLMAP = {
    "SYMBOL": "symbol",
    "SECTOR": "sector",
    "LISTED IN": "listed_in",
    "LDCP": "ldcp",
    "OPEN": "open",
    "HIGH": "high",
    "LOW": "low",
    "CURRENT": "current",
    "CHANGE": "change",
    "CHANGE (%)": "change_pct",
    "% CHANGE": "change_pct",
    "VOLUME": "volume",
    "TURNOVER": "turnover",
    "BID VOL.": "bid_vol",
    "BID PRICE": "bid_price",
    "OFFER PRICE": "offer_price",
    "OFFER VOL.": "offer_vol",
    "PRICE": "price",
    "MARKET CAP.": "market_cap",
    "30D VOLUME AVG.": "volume_avg_30d",
    "FREE FLOAT": "free_float",
}
NUMERIC = {
    "ldcp", "open", "high", "low", "current", "change", "change_pct", "volume",
    "turnover", "bid_vol", "bid_price", "offer_price", "offer_vol", "price",
    "market_cap", "volume_avg_30d", "free_float",
}

_TOKEN_RE = re.compile(r"window\.__ps\s*=\s*(\{.*?\})\s*(?:;|</script>)", re.DOTALL)
_SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9\-\.]*")


class PSXError(Exception):
    """Raised when the PSX portal can't be reached or read."""


def extract_token(html: str) -> str | None:
    m = _TOKEN_RE.search(html)
    if not m:
        return None
    try:
        tok = json.loads(m.group(1)).get("_k")
    except (ValueError, AttributeError):
        return None
    return tok if isinstance(tok, str) and len(tok) >= 16 else None


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    t = str(text).replace(",", "").replace("%", "").replace("PKR", "").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_table(html: str) -> pd.DataFrame:
    """Parse the first <table> on a PSX page into a DataFrame (header-driven)."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        return pd.DataFrame()
    head = table.find("thead") or table
    headers = [th.get_text(" ", strip=True).upper() for th in head.find_all("th")]
    cols = [COLMAP.get(h, re.sub(r"\W+", "_", h.lower()).strip("_")) for h in headers]
    body = table.find("tbody") or table
    rows = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        rec = {}
        for i, td in enumerate(tds[: len(cols)]):
            c = cols[i]
            if c == "symbol":
                a = td.find("a")
                raw = (a.get_text(" ", strip=True) if a else td.get_text(" ", strip=True)).upper()
                m = _SYMBOL_RE.match(raw)
                rec[c] = m.group(0) if m else raw
            else:
                # PSX sometimes puts the sortable raw value in data-order
                val = td.get("data-order") or td.get_text(" ", strip=True)
                rec[c] = _num(val) if c in NUMERIC else val
        rows.append(rec)
    df = pd.DataFrame(rows)
    if "symbol" in df.columns:
        df = df[df["symbol"].astype(str).str.len() > 0].drop_duplicates("symbol")
    return df.reset_index(drop=True)


class PSXClient:
    def __init__(self) -> None:
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._tok: str | None = None
        self._tok_at = 0.0
        self._lock = threading.Lock()

    # -- token -------------------------------------------------------------
    def _token(self, force: bool = False) -> str | None:
        with self._lock:
            if not force and self._tok and time.time() - self._tok_at < TOKEN_TTL:
                return self._tok
            try:
                r = self.s.get(BASE + "/", timeout=TIMEOUT)
                self._tok = extract_token(r.text) if r.ok else None
            except requests.RequestException:
                self._tok = None
            self._tok_at = time.time()
            return self._tok

    def _get(self, path: str, xhr: bool = True) -> str:
        last = ""
        for attempt in range(3):
            hdr = {}
            if xhr:
                hdr["X-Requested-With"] = "XMLHttpRequest"
                tok = self._token(force=attempt > 0)
                if tok:
                    hdr["X-Req-Id"] = tok
            try:
                r = self.s.get(BASE + path, headers=hdr, timeout=TIMEOUT)
            except requests.RequestException as e:
                last = f"network error: {e.__class__.__name__}"
                time.sleep(1 + attempt)
                continue
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 429) or r.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            break
        raise PSXError(f"PSX portal request failed for {path} ({last})")

    # -- data --------------------------------------------------------------
    def market_watch(self) -> pd.DataFrame:
        """Live prices for all regular-market stocks.

        Tries the market-watch page first (has open/high/low), then the
        regular-market trading board. Returns whatever columns PSX provides.
        """
        errors = []
        for path, xhr in (("/market-watch", True), ("/trading-board/REG/main", True)):
            try:
                df = parse_table(self._get(path, xhr=xhr))
                if not df.empty and {"symbol", "current"} <= set(df.columns):
                    df.attrs["source"] = path
                    return df
                errors.append(f"{path}: no price table")
            except PSXError as e:
                errors.append(str(e))
        raise PSXError("; ".join(errors))

    def screener(self) -> pd.DataFrame:
        """All listed stocks with 30-day average volume, sector, index membership."""
        return parse_table(self._get("/screener"))

    def history(self, symbol: str) -> pd.DataFrame:
        """Daily OHLCV for one symbol (all available history), oldest first."""
        last = ""
        for attempt in range(3):
            tok = self._token(force=attempt > 0)
            hdr = {"X-Requested-With": "XMLHttpRequest",
                   "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
            if tok:
                hdr["X-Req-Id"] = tok
            try:
                r = self.s.post(BASE + "/historical", data={"symbol": symbol}, headers=hdr, timeout=TIMEOUT)
            except requests.RequestException as e:
                last = e.__class__.__name__
                time.sleep(1 + attempt)
                continue
            if r.status_code == 200:
                df = parse_table(r.text)
                if df.empty or "date" not in df.columns:
                    return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
                df["date"] = pd.to_datetime(df["date"], format="%b %d, %Y", errors="coerce")
                for c in ("open", "high", "low", "close", "volume"):
                    df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
                df = df.dropna(subset=["date", "close"])
                df = df[df["close"] > 0]
                return df.sort_values("date").reset_index(drop=True)
            last = f"HTTP {r.status_code}"
            time.sleep(1 + attempt)
        raise PSXError(f"PSX history failed for {symbol} ({last})")

    def index_members(self, name: str = "KSE100") -> list[str]:
        df = parse_table(self._get(f"/indices/{name}"))
        return df["symbol"].tolist() if "symbol" in df.columns else []
