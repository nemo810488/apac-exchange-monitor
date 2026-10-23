"""Sanity-check observations.csv. Exit code 1 on hard errors, 0 otherwise.

Hard errors: unknown exchange/segment/metric, non-numeric values, duplicate keys,
             negative turnover/contracts, shares outside [0,1], month malformed.
Warnings:    participant shares that do not sum to ~1, turnover without trading_days,
             month-on-month jumps > 4x (likely a unit error), ALL product smaller than a component.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS = ROOT / "observations.csv"
METRICS = {"turnover_notional", "turnover_premium", "contracts", "trading_days",
           "share_retail", "share_foreign", "share_local_inst", "share_prop"}
SHARES = {m for m in METRICS if m.startswith("share_")}
SEGMENTS = {"stocks", "futures", "options"}


def main() -> int:
    venues = {}
    with (ROOT / "venues.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            venues[r["exchange"]] = r
    with OBS.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    errors, warns = [], []
    seen = set()
    series = defaultdict(dict)   # (exchange, segment, product, metric, basis) -> month -> value
    for i, r in enumerate(rows, start=2):
        where = f"line {i} ({r.get('exchange')} {r.get('segment')} {r.get('product')} {r.get('metric')} {r.get('month')})"
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", r["month"] or ""):
            errors.append(f"{where}: bad month")
        if r["exchange"] not in venues:
            errors.append(f"{where}: unknown exchange")
        elif r["country"] != venues[r["exchange"]]["country"]:
            errors.append(f"{where}: country {r['country']} does not match venues.csv")
        if r["segment"] not in SEGMENTS:
            errors.append(f"{where}: bad segment")
        if r["metric"] not in METRICS:
            errors.append(f"{where}: bad metric")
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            errors.append(f"{where}: non-numeric value {r['value']!r}")
            continue
        if r["metric"] in SHARES:
            if not (0 <= v <= 1):
                errors.append(f"{where}: share {v} outside [0,1]")
            if r["basis"] not in {"value", "contracts", "premium"}:
                errors.append(f"{where}: share without basis")
        elif v < 0:
            errors.append(f"{where}: negative value")
        if r["metric"].startswith("turnover_") and not r["currency"]:
            errors.append(f"{where}: turnover without currency")
        if r["method"] not in {"auto", "manual"}:
            errors.append(f"{where}: method must be auto|manual")
        k = (r["month"], r["exchange"], r["segment"], r["product"], r["metric"], r["basis"])
        if k in seen:
            errors.append(f"{where}: duplicate key")
        seen.add(k)
        series[k[1:]][r["month"]] = v

    # shares sum to ~1
    groups = defaultdict(dict)
    for (ex, seg, prod, met, basis), by_month in series.items():
        if met in SHARES:
            for m, v in by_month.items():
                groups[(m, ex, seg, prod, basis)][met] = v
    for g, parts in groups.items():
        if len(parts) == 4:
            s = sum(parts.values())
            if abs(s - 1) > 0.02:
                warns.append(f"shares sum to {s:.3f} for {g}")
        else:
            warns.append(f"incomplete share set ({len(parts)}/4) for {g}")

    # turnover without trading days; big jumps
    days = {(ex, seg): bm for (ex, seg, prod, met, basis), bm in series.items() if met == "trading_days" and prod == "ALL"}
    for (ex, seg, prod, met, basis), bm in series.items():
        if met in {"turnover_notional", "turnover_premium", "contracts"}:
            d = days.get((ex, seg)) or next((v for (e2, s2), v in days.items() if e2 == ex), None)
            if d is None:
                warns.append(f"no trading_days for {ex} {seg}; ADV cannot be derived")
            months = sorted(bm) if prod == "ALL" else []   # jump check only on headline series
            for a, b in zip(months, months[1:]):
                if bm[a] > 0 and bm[b] > 0:
                    ratio = bm[b] / bm[a]
                    if ratio > 4 or ratio < 0.25:
                        warns.append(f"{ex} {seg} {prod} {met}: {a}->{b} changed {ratio:.1f}x (unit error?)")
    # ALL >= component
    for (ex, seg, prod, met, basis), bm in series.items():
        if prod != "ALL" and met in {"turnover_notional", "turnover_premium", "contracts"}:
            allbm = series.get((ex, seg, "ALL", met, basis), {})
            for m, v in bm.items():
                if m in allbm and v > allbm[m] * 1.0001:
                    warns.append(f"{ex} {seg} {prod} {met} {m}: component {v:.4g} > ALL {allbm[m]:.4g}")

    for w in warns:
        print("WARN", w)
    for e in errors:
        print("ERROR", e)
    print(f"{len(rows)} rows, {len(errors)} errors, {len(warns)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
