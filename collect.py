"""Run the automated collectors and merge results into observations.csv

    python3 collect.py                 # last 13 complete months, all collectors
    MONTHS=3 python3 collect.py        # re-collect only the last 3 months
    ONLY=tw,jp python3 collect.py      # subset of collectors

Merge rules (key = month, exchange, segment, product, metric, basis):
  * new automated rows replace older automated rows for the same key
  * rows from manual_*.csv (method=manual) always win over automated rows,
    so a manual correction is never clobbered by a re-run
  * nothing is ever deleted: months outside the collection window are kept
"""
from __future__ import annotations

import csv
import importlib
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from common import last_n_months  # noqa: E402

log = logging.getLogger("collect")
OBS = ROOT / "observations.csv"
MANUAL_DIR = ROOT
FIELDS = ["month", "country", "exchange", "segment", "product", "metric", "basis",
          "value", "currency", "source", "method", "collected_at"]
KEY = ("month", "exchange", "segment", "product", "metric", "basis")


def _key(r: dict) -> tuple:
    return tuple(str(r.get(k, "") or "") for k in KEY)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["month"], r["country"], r["exchange"], r["segment"],
                                       r["product"], r["metric"], r["basis"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            v = r.get("value", "")
            try:
                fv = float(v)
                r["value"] = f"{fv:.6f}".rstrip("0").rstrip(".") if abs(fv) < 1 else f"{fv:.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                pass
            w.writerow(r)


def collectors_to_run() -> list[str]:
    only = os.environ.get("ONLY")
    if only is not None and only.strip().lower() == "none":
        return []  # merge manual CSVs only (used by the validate workflow)
    if only:
        return [x.strip() for x in only.split(",") if x.strip()]
    names = set()
    with (ROOT / "venues.csv").open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["collector"] and r["status"] == "auto":
                names.add(r["collector"])
    return sorted(names)


def load_manual() -> list[dict]:
    rows: list[dict] = []
    for p in sorted(MANUAL_DIR.glob("manual_*.csv")):
        for r in read_csv(p):
            if not r.get("month"):
                continue
            r["method"] = "manual"
            r.setdefault("basis", "")
            r.setdefault("collected_at", "")
            rows.append(r)
    return rows


def main() -> int:
    months = last_n_months(int(os.environ.get("MONTHS", "13")))
    log.info("months: %s .. %s", months[0], months[-1])
    existing = {_key(r): r for r in read_csv(OBS)}
    failures = []
    for name in collectors_to_run():
        try:
            mod = importlib.import_module(name)
            got = mod.collect(months)
            log.info("%s: %d rows", name, len(got))
            for r in got:
                k = _key(r)
                prev = existing.get(k)
                if prev and prev.get("method") == "manual":
                    continue  # manual wins
                existing[k] = r
        except Exception as e:  # noqa: BLE001
            log.error("collector %s failed: %s", name, e)
            failures.append(name)
    for r in load_manual():
        existing[_key(r)] = r
    write_csv(OBS, list(existing.values()))
    log.info("wrote %d rows to %s", len(existing), OBS.relative_to(ROOT))
    if failures:
        log.error("collectors with errors: %s", ", ".join(failures))
    return 0  # never fail the run; validate.py decides what is fatal


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
