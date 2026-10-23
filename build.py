"""Compile observations.csv + fx_monthly.csv + venues.csv into
data.json (read by the dashboard) and summary_latest.csv (human table).

Derived per (exchange, segment, product, month):
    adv_notional_local = turnover_notional / trading_days
    adv_premium_local  = turnover_premium  / trading_days
    adv_contracts      = contracts         / trading_days
    *_usd              = *_local / per_usd(month, currency)
trading_days is taken from the same exchange+segment (product ALL), falling back to
any trading_days row for that exchange in that month.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OBS = ROOT / "observations.csv"
FX = ROOT / "fx_monthly.csv"
VENUES = ROOT / "venues.csv"
OUT_JSON = ROOT / "data.json"
OUT_SUMMARY = ROOT / "summary_latest.csv"


def read(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> None:
    venues = read(VENUES)
    obs = read(OBS)
    fx: dict[str, dict[str, float]] = defaultdict(dict)
    if FX.exists():
        for r in read(FX):
            fx[r["month"]][r["currency"]] = float(r["per_usd"])

    months = sorted({r["month"] for r in obs})
    series: dict[tuple, dict] = {}
    for r in obs:
        k = (r["country"], r["exchange"], r["segment"], r["product"], r["metric"], r["basis"])
        s = series.setdefault(k, {
            "country": r["country"], "exchange": r["exchange"], "segment": r["segment"],
            "product": r["product"], "metric": r["metric"], "basis": r["basis"],
            "currency": r["currency"], "points": {}, "sources": {}, "method": {},
        })
        s["points"][r["month"]] = float(r["value"])
        s["sources"][r["month"]] = r["source"]
        s["method"][r["month"]] = r["method"]

    # trading days lookup
    tdays: dict[tuple, dict[str, float]] = defaultdict(dict)
    for s in series.values():
        if s["metric"] == "trading_days":
            for m, v in s["points"].items():
                tdays[(s["exchange"], s["segment"])][m] = v
                tdays[(s["exchange"], "*")].setdefault(m, v)

    def days_for(ex: str, seg: str, m: str):
        return tdays.get((ex, seg), {}).get(m) or tdays.get((ex, "*"), {}).get(m)

    derived: dict[tuple, dict] = {}
    for s in series.values():
        if s["metric"] not in {"turnover_notional", "turnover_premium", "contracts"}:
            continue
        for m, v in s["points"].items():
            d = days_for(s["exchange"], s["segment"], m)
            if not d:
                continue
            k = (s["exchange"], s["segment"], s["product"], m)
            rec = derived.setdefault(k, {
                "country": s["country"], "exchange": s["exchange"], "segment": s["segment"],
                "product": s["product"], "month": m, "trading_days": d, "currency": s["currency"] or None,
            })
            adv = v / d
            if s["currency"]:
                rec["currency"] = s["currency"]
            rate = fx.get(m, {}).get(s["currency"]) if s["currency"] else None
            if s["metric"] == "turnover_notional":
                rec["adv_notional_local"] = adv
                rec["adv_notional_usd"] = adv / rate if rate else None
            elif s["metric"] == "turnover_premium":
                rec["adv_premium_local"] = adv
                rec["adv_premium_usd"] = adv / rate if rate else None
            else:
                rec["adv_contracts"] = adv

    # participant shares attached to derived records (ALL and per product)
    for s in series.values():
        if not s["metric"].startswith("share_"):
            continue
        for m, v in s["points"].items():
            k = (s["exchange"], s["segment"], s["product"], m)
            rec = derived.get(k)
            if rec is None:
                continue
            rec.setdefault("shares", {})[s["metric"][6:]] = v
            rec["share_basis"] = s["basis"]

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "months": months,
        "venues": venues,
        "fx": fx,
        "series": list(series.values()),
        "derived": sorted(derived.values(), key=lambda r: (r["country"], r["exchange"], r["segment"], r["product"], r["month"])),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # human-readable latest-month table (ALL products only)
    latest = months[-1] if months else ""
    with OUT_SUMMARY.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "country", "exchange", "segment", "trading_days", "currency",
                    "adv_notional_local_bn", "adv_notional_usd_bn", "adv_premium_usd_bn", "adv_contracts",
                    "retail", "foreign", "local_inst", "prop", "share_basis"])
        for r in out["derived"]:
            if r["product"] != "ALL":
                continue
            # pick the latest month available for this venue/segment
            same = [x for x in out["derived"] if x["exchange"] == r["exchange"] and x["segment"] == r["segment"] and x["product"] == "ALL"]
            if r["month"] != max(x["month"] for x in same):
                continue
            sh = r.get("shares", {})
            fmt = lambda x, d=1e9: f"{x / d:.3f}" if x else ""  # noqa: E731
            w.writerow([r["month"], r["country"], r["exchange"], r["segment"], int(r["trading_days"]), r["currency"] or "",
                        fmt(r.get("adv_notional_local")), fmt(r.get("adv_notional_usd")), fmt(r.get("adv_premium_usd")),
                        f"{r['adv_contracts']:.0f}" if r.get("adv_contracts") else "",
                        *(f"{sh[k]:.3f}" if k in sh else "" for k in ("retail", "foreign", "local_inst", "prop")),
                        r.get("share_basis", "")])
    print(f"wrote {OUT_JSON.relative_to(ROOT)} ({OUT_JSON.stat().st_size // 1024} KB), "
          f"{len(out['series'])} series, {len(out['derived'])} derived rows, months {months[0] if months else '-'}..{latest}")


if __name__ == "__main__":
    main()
