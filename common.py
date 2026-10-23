"""Shared helpers for all collectors.

Every collector module exposes:

    VENUES: list[str]                      # exchange codes it produces rows for
    def collect(months: list[str]) -> list[dict]

`months` are "YYYY-MM" strings. Each returned row is one observation in the
tidy schema below (see README "Data schema"). Collectors must be idempotent
and must never raise on a single missing month: log and skip instead.

Row schema (all keys required, use "" for not-applicable):
    month        "YYYY-MM"
    country      ISO-3166 alpha-2: CN HK JP TW KR AU TH VN IN SG MY
    exchange     venue code from venues.csv (e.g. TWSE, TAIFEX, OSE, KRX)
    segment      stocks | futures | options
    product      "ALL" for the whole segment, else a product-group code
                 (e.g. TX, MTX, TXO, NK225F, NK225M, KOSPI200F, KOSPI200O)
    metric       turnover_notional   total monthly traded value, notional basis
                 turnover_premium    total monthly traded value, premium basis (options)
                 contracts           total monthly contracts traded (derivatives)
                 trading_days        number of trading days in the month
                 share_retail        fraction 0-1 of (buy+sell) activity by local individuals
                 share_foreign       fraction 0-1 by foreign investors (all types)
                 share_local_inst    fraction 0-1 by local institutions (funds, insurers, banks, corporates...)
                 share_prop          fraction 0-1 by securities/futures dealers' proprietary desks (incl. market makers)
    basis        for share_* only: value | contracts | premium  (what the share is measured on); else ""
    value        number, in base currency units (not millions) or raw counts / fractions
    currency     ISO code for turnover_* rows, else ""
    source       URL of the page/file the number came from
    method       auto | manual
    collected_at ISO date (UTC) the row was produced
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Iterable

import requests

log = logging.getLogger("collectors")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 apac-exchange-monitor/0.1"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en,zh-TW;q=0.8,ja;q=0.7,ko;q=0.6"})

# Sandbox/corporate proxies may need a custom CA bundle.
_CA = os.environ.get("REQUESTS_CA_BUNDLE") or "/root/.ccr/ca-bundle.crt"
if os.path.exists(_CA):
    SESSION.verify = _CA


def get(url: str, *, params=None, retries: int = 3, timeout: int = 60, **kw) -> requests.Response:
    """GET with retries and polite backoff."""
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout, **kw)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last = RuntimeError(f"{r.status_code} from {url}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last}")


def post(url: str, *, data=None, json=None, retries: int = 3, timeout: int = 60, **kw) -> requests.Response:
    last = None
    for attempt in range(retries):
        try:
            r = SESSION.post(url, data=data, json=json, timeout=timeout, **kw)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last = RuntimeError(f"{r.status_code} from {url}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"POST failed after {retries} tries: {url}: {last}")


def row(*, month, country, exchange, segment, product, metric, value, source,
        currency="", basis="", method="auto") -> dict:
    return {
        "month": month,
        "country": country,
        "exchange": exchange,
        "segment": segment,
        "product": product,
        "metric": metric,
        "basis": basis,
        "value": float(value),
        "currency": currency,
        "source": source,
        "method": method,
        "collected_at": dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }


def last_n_months(n: int = 12, end: dt.date | None = None) -> list[str]:
    """Return the n complete months ending with the month *before* `end` (default today)."""
    end = end or dt.date.today()
    first_of_this = end.replace(day=1)
    out = []
    y, m = first_of_this.year, first_of_this.month
    for _ in range(n):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        out.append(f"{y:04d}-{m:02d}")
    return sorted(out)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    y, m = (int(x) for x in month.split("-"))
    start = dt.date(y, m, 1)
    end = (dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)) - dt.timedelta(days=1)
    return start, end


def to_num(s) -> float:
    """Parse '1,234.5', '(12)', '-', '' etc."""
    if s is None:
        raise ValueError("None")
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "").replace("　", "")
    if t in {"", "-", "—", "–", "N/A", "n/a"}:
        raise ValueError(f"empty numeric: {s!r}")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    v = float(t)
    return -v if neg else v


def shares_from_parts(parts: dict[str, float]) -> dict[str, float]:
    """Normalise a dict of bucket -> activity into fractions summing to 1."""
    total = sum(parts.values())
    if total <= 0:
        raise ValueError("non-positive total")
    return {k: v / total for k, v in parts.items()}
