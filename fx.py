"""Monthly average USD FX rates -> fx_monthly.csv

Columns: month, currency, usd_per_unit_inverse (units of currency per 1 USD), source, days

Sources (no API key):
  * Frankfurter (ECB reference rates): AUD CNY HKD INR JPY KRW MYR SGD THB
  * fawazahmed0/currency-api on jsDelivr (daily snapshots): TWD VND, and fallback for all
Rates are simple averages of the daily values inside the month.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common import get, last_n_months, month_bounds  # noqa: E402

log = logging.getLogger("fx")
OUT = ROOT / "fx_monthly.csv"
FRANKFURTER = {"AUD", "CNY", "HKD", "INR", "JPY", "KRW", "MYR", "SGD", "THB"}
ALL_CCY = sorted(FRANKFURTER | {"TWD", "VND"})


def _frankfurter(month: str, ccys: set[str]) -> dict[str, tuple[float, int]]:
    start, end = month_bounds(month)
    url = f"https://api.frankfurter.app/{start}..{end}"
    r = get(url, params={"from": "USD", "to": ",".join(sorted(ccys))})
    rates = r.json().get("rates", {})
    out: dict[str, tuple[float, int]] = {}
    for c in ccys:
        vals = [d[c] for d in rates.values() if c in d]
        if vals:
            out[c] = (sum(vals) / len(vals), len(vals))
    return out


def _jsdelivr(month: str, ccys: set[str]) -> dict[str, tuple[float, int]]:
    start, end = month_bounds(month)
    acc: dict[str, list[float]] = {c: [] for c in ccys}
    d = start
    while d <= end:
        if d.weekday() < 5:  # weekdays only, like ECB
            url = f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{d}/v1/currencies/usd.min.json"
            try:
                j = get(url, retries=2, timeout=30).json()["usd"]
                for c in ccys:
                    v = j.get(c.lower())
                    if v:
                        acc[c].append(float(v))
            except Exception as e:  # noqa: BLE001
                log.debug("no snapshot %s: %s", d, e)
        d += dt.timedelta(days=1)
    return {c: (sum(v) / len(v), len(v)) for c, v in acc.items() if v}


def load_existing() -> dict[tuple[str, str], dict]:
    if not OUT.exists():
        return {}
    with OUT.open() as f:
        return {(r["month"], r["currency"]): r for r in csv.DictReader(f)}


def update(months: list[str]) -> None:
    rows = load_existing()
    for m in months:
        need = {c for c in ALL_CCY if (m, c) not in rows}
        if not need:
            continue
        got: dict[str, tuple[float, int, str]] = {}
        fr = need & FRANKFURTER
        if fr:
            try:
                for c, (v, n) in _frankfurter(m, fr).items():
                    got[c] = (v, n, "frankfurter.app (ECB)")
            except Exception as e:  # noqa: BLE001
                log.warning("frankfurter failed for %s: %s", m, e)
        rest = need - set(got)
        if rest:
            try:
                for c, (v, n) in _jsdelivr(m, rest).items():
                    got[c] = (v, n, "fawazahmed0/currency-api")
            except Exception as e:  # noqa: BLE001
                log.warning("currency-api failed for %s: %s", m, e)
        for c, (v, n, src) in got.items():
            rows[(m, c)] = {"month": m, "currency": c, "per_usd": f"{v:.6f}", "days": str(n), "source": src}
        log.info("%s: %d currencies", m, len(got))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["month", "currency", "per_usd", "days", "source"])
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    n = int(os.environ.get("MONTHS", "13"))
    update(last_n_months(n))
