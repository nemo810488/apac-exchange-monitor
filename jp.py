"""Japan collector: TSE cash equities and OSE derivatives (JPX statistics).

Sources (all on www.jpx.co.jp; link discovery parses the index pages and their
"backnumber" year archives rather than hard-coding hashed attachment folders):

TSE, segment stocks
  * turnover_notional / trading_days
      Monthly Statistics Report, file "01_sokatu{yymm}.pdf" (Key Statistics for
      Domestic Stocks, table 1-1 "Combined total of Markets" = Prime + Standard +
      Growth + TOKYO PRO Market; domestic common stocks only, i.e. excludes ETFs,
      REITs and foreign stocks). Index:
        https://www.jpx.co.jp/english/markets/statistics-equities/monthly/index.html
      JPX publishes this table only as PDF (no Excel/CSV); it is machine generated
      and parsed with pdfplumber. Trading value is in MILLION yen -> x1e6.
      Each PDF carries ~24 months so the newest file normally covers everything.
  * share_* (basis value)
      Trading by Type of Investors, monthly Excel. Index:
        https://www.jpx.co.jp/english/markets/statistics-equities/investor-type/00-01.html
      Up to Aug 2026 one file per month per measure: "stock_val_1_m{yymm}.xls"
      (sheets 'TSE Prime', 'TSE Standard', 'TSE Growth', 'Tokyo & Nagoya').
      From Sep 2026 volume+value are combined in "stock_1_m{yyyymm}.xlsx" (one
      sheet, one row block per market with a 'Value' row). We sum the three TSE
      market sheets/rows (Prime+Standard+Growth) - there is no separate "three
      markets total" sheet; the 'Tokyo & Nagoya' sheet additionally includes the
      Nagoya SE, so it is not used. Values are in THOUSAND yen but only ratios
      are produced here. Survey coverage: trading participants with capital
      >= JPY 3bn; sales+purchases are both counted.
      Category -> bucket mapping (documented also in EQ_BUCKETS below):
        Proprietary                                  -> share_prop
        Brokerage / Individuals                      -> share_retail
        Brokerage / Foreigners (incl. foreign indiv.)-> share_foreign
        Brokerage / Securities Cos.                  -> share_local_inst
        Brokerage / Institutions: Investment Trusts, Business Cos., Other Cos.,
          Life & Non-Life, City & Regional BK, Trust BK, Other Financials
                                                     -> share_local_inst

OSE, segments futures / options
  * contracts / turnover_notional (futures) / turnover_premium (options) /
    trading_days
      Statistics (Derivatives) > Summary of Statistics:
        https://www.jpx.co.jp/english/markets/statistics-derivatives/trading-volume/index.html
      File "tv_ts{yyyymm}.xls" (Changes of Trading Volume and Trading Value,
      monthly sheet, 1985-present, all products incl. TOCOM commodities, auction +
      J-NET). Volume in contracts, value in YEN (no scaling). Options "trading
      value" is premium-based (JPX convention) -> turnover_premium.
      Product ALL for futures / options = sum of all per-product columns of that
      kind (checked against the file's own grand Total column).
      Fallback (stable URL, no totals): /automation/markets/statistics-derivatives/
      trading-volume/files/soukatsu_M.xlsx.
  * share_* (basis contracts and basis value for named products; basis value for
    ALL = sum over all futures or all options product codes)
      Trading by Type of Investors (derivatives), monthly CSV:
        https://www.jpx.co.jp/english/markets/statistics-derivatives/sector/01.html
      File "Tousi_DV_M_{yyyymm}[_mmdd_mmdd].csv" (UTF-8 BOM, coded columns; codes
      from the JPX guide_20260413.xls). NOTE: JPX "monthly" investor data is the
      sum of the ISO-style reporting weeks assigned to the month, so the covered
      period can start/end a few days outside the calendar month.
      Investor code -> bucket: 11 Proprietary -> prop; 51 Individuals -> retail;
      60 Foreigners -> foreign; 41 Securities Cos., 31 Investment Trusts,
      32 Business Cos., 33 Other Institutions, 21 Insurance, 22 City & Regional
      banks, 23 Trust banks, 24 Other financials -> local_inst.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import re
import time
from html import unescape
from urllib.parse import urljoin

import pandas as pd

import common
from common import row, to_num

log = logging.getLogger("collectors.jp")

VENUES = ["TSE", "OSE"]
COUNTRY = "JP"
CCY = "JPY"
BASE = "https://www.jpx.co.jp"
RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "jp")
POLITE_SLEEP = 0.8
INDEX_TTL_SEC = 24 * 3600  # re-fetch index pages / stable "latest" files after a day

EQ_MONTHLY_INDEX = f"{BASE}/english/markets/statistics-equities/monthly/index.html"
EQ_INVESTOR_INDEX = f"{BASE}/english/markets/statistics-equities/investor-type/00-01.html"
DV_SUMMARY_INDEX = f"{BASE}/english/markets/statistics-derivatives/trading-volume/index.html"
DV_SUMMARY_FALLBACK = f"{BASE}/automation/markets/statistics-derivatives/trading-volume/files/soukatsu_M.xlsx"
DV_INVESTOR_INDEX = f"{BASE}/english/markets/statistics-derivatives/sector/01.html"

# ----------------------------------------------------------------------------
# Equity investor-type category mapping (English labels as they appear in the
# JPX files) -> share bucket.
EQ_BUCKETS = {
    "Proprietary": "share_prop",
    "Individuals": "share_retail",
    "Foreigners": "share_foreign",
    "Securities Cos.": "share_local_inst",
    "Investment Trusts": "share_local_inst",
    "Business Cos.": "share_local_inst",
    "Other Cos.": "share_local_inst",
    "Life & Non-Life": "share_local_inst",
    "City & Regional BK": "share_local_inst",
    "Trust BK": "share_local_inst",
    "Other Financials": "share_local_inst",
}
# Old-format .xls: label fragments found in column 0 (sometimes split over rows).
EQ_OLD_LABELS = {
    "Proprietary": "Proprietary",
    "Individuals": "Individuals",
    "Foreigners": "Foreigners",
    "Securities Cos.": "Securities Cos.",
    "Investment": "Investment Trusts",
    "Trusts": "Investment Trusts",
    "Business Cos.": "Business Cos.",
    "Other Cos.": "Other Cos.",
    "Life & Non-Life": "Life & Non-Life",
    "City & Regional BK": "City & Regional BK",
    "Trust BK": "Trust BK",
    "Other Financials": "Other Financials",
    "Total": "Total",  # grand total, used only as a consistency check
}
EQ_TSE_SHEETS = ("TSE Prime", "TSE Standard", "TSE Growth")

# Derivatives investor-type: investor code -> bucket
DV_INVESTOR_BUCKET = {
    "11": "share_prop",
    "51": "share_retail",
    "60": "share_foreign",
    "41": "share_local_inst", "31": "share_local_inst", "32": "share_local_inst",
    "33": "share_local_inst", "21": "share_local_inst", "22": "share_local_inst",
    "23": "share_local_inst", "24": "share_local_inst",
}
# Subtotal codes present in the pre-2026-04 layout (ignored, not leaves)
DV_AGGREGATE_CODES = {"70", "80", "30", "20"}
# Product-type codes (guide_20260413.xls). Options are listed per put/call code.
DV_OPTION_CODES = {
    "303", "304",  # Nikkei 225 Options
    "332", "333",  # Nikkei 225 mini Options
    "319", "320",  # TOPIX Options
    "327", "328",  # JPX-Nikkei 400 Options
    "439", "440",  # TOPIX Banks Index Options
    "441", "442",  # TSE REIT Index Options
    "321", "322",  # Options on 10y JGB Futures
    "307", "308",  # Securities Options
    "413", "414",  # Options on Gold Futures
}
DV_INVESTOR_PRODUCTS = {
    # product code -> (segment, [type codes])
    "NK225F": ("futures", ["301"]),
    "NK225M": ("futures", ["313"]),
    "NK225MC": ("futures", ["331"]),
    "TOPIXF": ("futures", ["314"]),
    "TOPIXM": ("futures", ["316"]),
    "JGBF": ("futures", ["317"]),
    "NK225O": ("options", ["303", "304"]),
    "NK225MO": ("options", ["332", "333"]),
    "TOPIXO": ("options", ["319", "320"]),
}

# tv_ts column header (English part, lower-cased, whitespace-normalised) -> product code
DV_VOLUME_PRODUCTS = {
    "nikkei 225 futures": ("futures", "NK225F"),
    "nikkei 225 mini": ("futures", "NK225M"),
    "nikkei 225 micro futures": ("futures", "NK225MC"),
    "topix futures": ("futures", "TOPIXF"),
    "mini topix futures": ("futures", "TOPIXM"),
    "jpx-nikkei index 400 futures": ("futures", "JPXN400F"),
    "tse growth market 250 index futures": ("futures", "GROWTH250F"),
    "topix banks index futures": ("futures", "TOPIXBF"),
    "tse reit index futures": ("futures", "REITF"),
    "ose djia futures": ("futures", "DJIAF"),
    "nikkei vi futures": ("futures", "NK225VIF"),
    "10-year jgb futures futures": ("futures", "JGBF"),  # sic: header says "Futures Futures"
    "10-year jgb futures": ("futures", "JGBF"),
    "mini-10-year jgb futures (cash-settled)": ("futures", "JGB10MF"),
    "mini-20-year jgb futures": ("futures", "JGB20MF"),
    "3-month tona futures": ("futures", "TONAF"),
    "gold standard futures": ("futures", "GOLDF"),
    "gold mini futures": ("futures", "GOLDMF"),
    "platinum standard futures": ("futures", "PLATF"),
    "rss3 rubber futures": ("futures", "RUBBERF"),
    "platts dubai crude oil futures": ("futures", "DUBAIF"),
    "nikkei 225 options": ("options", "NK225O"),
    "nikkei 225 mini options": ("options", "NK225MO"),
    "topix options": ("options", "TOPIXO"),
    "options on jgb(10-year) futures": ("options", "JGBO"),
    "securities options": ("options", "SECO"),
}


# ----------------------------------------------------------------------------
# Fetch / cache helpers
def _cache_path(url: str) -> str:
    """raw/jp/<section>__<attachment-folder>__<file>, derived from the URL path.

    The JPX attachment folder (e.g. vk0khi000002qu3s-att) is kept because the same
    file name can appear under different folders in different years.
    """
    path = re.sub(r"^https?://[^/]+", "", url).strip("/")
    path = re.sub(r"^(english/markets/|automation/markets/)", "", path)
    parts = [p for p in path.split("/") if p]
    if len(parts) > 1 and (parts[-2].endswith("-att") or parts[-2] == "files"):
        name = "__".join(parts[:-2] + [parts[-2], parts[-1]])
    else:
        name = "__".join(parts)
    name = name.replace("statistics-", "").replace("/", "__")
    return os.path.join(RAW_DIR, re.sub(r"[^A-Za-z0-9_.\-]", "_", name))


def _fetch(url: str, *, ttl: float | None = None) -> bytes:
    """GET with on-disk cache under raw/jp. ttl=None -> cache forever."""
    os.makedirs(RAW_DIR, exist_ok=True)
    p = _cache_path(url)
    if os.path.exists(p):
        age = time.time() - os.path.getmtime(p)
        if ttl is None or age < ttl:
            with open(p, "rb") as f:
                return f.read()
    log.info("GET %s", url)
    r = common.get(url)
    time.sleep(POLITE_SLEEP)
    with open(p, "wb") as f:
        f.write(r.content)
    return r.content


def _fetch_html(url: str) -> str:
    return _fetch(url, ttl=INDEX_TTL_SEC).decode("utf-8", errors="replace")


_HREF_RX = re.compile(r'href="([^"]+)"', re.I)
_OPTION_RX = re.compile(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', re.I)


def _year_archives(index_html: str, index_url: str, years: set[int]) -> list[str]:
    """Return archive-page URLs from the page's year <select> for the wanted years."""
    out = []
    for href, label in _OPTION_RX.findall(index_html):
        label = unescape(label).strip()
        if re.fullmatch(r"\d{4}", label) and int(label) in years and href:
            u = urljoin(index_url, href)
            if u != index_url:
                out.append(u)
    return out


def _discover(index_url: str, pattern: str, years: set[int]) -> dict[str, str]:
    """Scan index page (+ year archives) for hrefs matching `pattern`.

    `pattern` must have one capture group used as the dict key (e.g. yymm).
    Later pages never override keys found on the current page.
    """
    rx = re.compile(pattern)
    found: dict[str, str] = {}
    pages = [index_url]
    try:
        html = _fetch_html(index_url)
    except Exception as e:  # noqa: BLE001
        log.warning("index page failed %s: %s", index_url, e)
        return found
    pages += _year_archives(html, index_url, years)
    seen = set()
    for i, pu in enumerate(pages):
        if pu in seen:
            continue
        seen.add(pu)
        try:
            h = html if i == 0 else _fetch_html(pu)
        except Exception as e:  # noqa: BLE001
            log.warning("archive page failed %s: %s", pu, e)
            continue
        for href in _HREF_RX.findall(h):
            m = rx.search(href)
            if m:
                found.setdefault(m.group(1), urljoin(pu, href))
    return found


def _yymm(month: str) -> str:
    return month[2:4] + month[5:7]


def _yyyymm(month: str) -> str:
    return month[:4] + month[5:7]


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _num0(v) -> float:
    """to_num() that maps blanks / NaN / '－' to 0.0."""
    try:
        x = to_num(v)
    except ValueError:
        return 0.0
    return 0.0 if x != x else x  # NaN check


# ----------------------------------------------------------------------------
# TSE: monthly total trading value / trading days from 01_sokatu PDF
_SOKATU_ROW_RX = re.compile(r"^\s*(20\d\d)\s+(\d{1,2})\s+(\d{1,2})\s+(.*)$")


def _parse_sokatu_pdf(data: bytes) -> dict[str, dict]:
    """Return {month: {days, volume_thousand_shs, value_jpy}} from table 1-1 (first page)."""
    import pdfplumber  # local import: only needed for TSE

    out: dict[str, dict] = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = pdf.pages[0].extract_text() or ""
    if "Combined total of Markets" not in text:
        log.warning("sokatu PDF page 1 is not the combined-markets table")
    for line in text.splitlines():
        m = _SOKATU_ROW_RX.match(line)
        if not m:
            continue
        y, mo, days = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not 1 <= mo <= 12:
            continue
        toks = m.group(4).split()
        # [ListedCos, NewlyListed, Issues, SharesListed, MktCap, VolTotal, VolAvg,
        #  VolHigh, Date, VolLow, Date, ValTotal, ValAvg, ValHigh, Date, ValLow, Date, ...]
        try:
            vol_total = to_num(toks[5])
            val_total_mil = to_num(toks[11])
            val_avg_mil = to_num(toks[12])
        except (IndexError, ValueError) as e:
            log.warning("sokatu row parse failed (%s): %s", e, line[:80])
            continue
        # column-shift guard: total ~= daily average * trading days
        if days and abs(val_avg_mil * days - val_total_mil) / val_total_mil > 0.02:
            log.warning("sokatu row failed consistency check: %s", line[:80])
            continue
        out[f"{y:04d}-{mo:02d}"] = {
            "days": days,
            "volume_thousand_shs": vol_total,
            "value_jpy": val_total_mil * 1_000_000,  # file unit: million yen
        }
    return out


def _collect_tse_turnover(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    years = {int(m[:4]) for m in months}
    files = _discover(EQ_MONTHLY_INDEX, r"01_sokatu(\d{4})\.pdf", years)
    if not files:
        log.error("TSE: no 01_sokatu PDFs discovered on %s", EQ_MONTHLY_INDEX)
        return rows
    parsed: dict[str, dict[str, dict]] = {}  # url -> month -> data

    def get(url):
        if url not in parsed:
            try:
                parsed[url] = _parse_sokatu_pdf(_fetch(url))
            except Exception as e:  # noqa: BLE001
                log.warning("TSE: failed to parse %s: %s", url, e)
                parsed[url] = {}
        return parsed[url]

    latest_key = max(files)  # yymm sorts correctly within a century
    for month in months:
        src = None
        data = None
        for key in (latest_key, _yymm(month)):
            if key in files:
                d = get(files[key]).get(month)
                if d:
                    src, data = files[key], d
                    break
        if not data:
            log.warning("TSE %s: not found in any 01_sokatu PDF", month)
            continue
        rows.append(row(month=month, country=COUNTRY, exchange="TSE", segment="stocks", product="ALL",
                        metric="turnover_notional", value=data["value_jpy"], currency=CCY, source=src))
        rows.append(row(month=month, country=COUNTRY, exchange="TSE", segment="stocks", product="ALL",
                        metric="trading_days", value=data["days"], source=src))
    return rows


# ----------------------------------------------------------------------------
# TSE: investor-type shares
def _eq_old_sheet_totals(df: pd.DataFrame) -> dict[str, float]:
    """Old .xls layout: label in col 0 (possibly on the row above), 'Total' in col 2, value in col 4."""
    totals: dict[str, float] = {}
    label = None
    for _, r in df.iterrows():
        c0 = str(r.iloc[0]).strip() if pd.notna(r.iloc[0]) else ""
        if c0 in EQ_OLD_LABELS:
            label = EQ_OLD_LABELS[c0]
        c2 = str(r.iloc[2]).strip() if len(r) > 2 and pd.notna(r.iloc[2]) else ""
        if c2 == "Total" and label and label not in totals:
            try:
                totals[label] = to_num(r.iloc[4])
            except ValueError:
                pass
            label = None
    return totals


def _eq_new_sheet_totals(df: pd.DataFrame, markets=("TSE Prime", "TSE Standard", "TSE Growth")) -> dict[str, float]:
    """New .xlsx layout (Sep 2026+): header rows 3-5 name categories, each with
    Sales/Purchases/Balance/Total; one 'Value' row per market."""
    # find header row with 'Sales'/'Total' cells
    hdr_row = None
    for i in range(min(12, len(df))):
        vals = [_norm(v) for v in df.iloc[i] if pd.notna(v)]
        if any(v.endswith("total") for v in vals) and any("sales" in v for v in vals):
            hdr_row = i
            break
    if hdr_row is None:
        raise ValueError("new-format header row not found")
    def english(s) -> str:
        # cells look like '自己 Proprietary' or '生保・損保 Life & Non-Life' or '証券会社 Securities Cos.'
        s = str(s).replace("\xa0", " ").strip()
        m = re.search(r"[A-Za-z][A-Za-z&./ \-]*$", s)
        return m.group(0).strip() if m else s

    # columns whose header is 'Total'; each category block is 4 columns wide starting at total_col-3
    total_cols = [c for c, v in enumerate(df.iloc[hdr_row]) if pd.notna(v) and _norm(v).endswith("total")]
    # category labels are merged cells (value only in the block's first column): forward-fill
    labels = df.iloc[:hdr_row, :].copy()
    labels = labels.ffill(axis=1)
    col_cat: dict[int, str] = {}
    for tc in total_cols:
        start = tc - 3
        # label chain from the row just above the header upwards (duplicates collapsed)
        chain: list[str] = []
        for i in range(hdr_row - 1, 0, -1):  # row 0 is the sheet title
            v = labels.iloc[i, start] if start < labels.shape[1] else None
            if pd.notna(v):
                e = english(v)
                if e and (not chain or chain[-1] != e):
                    chain.append(e)
        if not chain:
            continue
        lab = chain[0]
        parent = chain[1] if len(chain) > 1 else ""
        # 'Cash'/'Margin' sit under Proprietary or Individuals; Foreigners is split into
        # Institutions / Individuals, which we keep together as Foreigners.
        if lab in ("Cash", "Margin"):
            cat = parent
        elif parent.startswith("Foreigners"):
            cat = "Foreigners"
        else:
            cat = lab
        col_cat[tc] = cat

    totals: dict[str, float] = {}
    market = None
    for i in range(hdr_row + 1, len(df)):
        r = df.iloc[i]
        if pd.notna(r.iloc[1]):
            market = str(r.iloc[1])
        kind = _norm(r.iloc[2]) if pd.notna(r.iloc[2]) else ""
        if not kind.endswith("value") or market is None:
            continue
        if not any(mk.lower() in market.lower() for mk in markets):
            continue
        for tc, cat in col_cat.items():
            try:
                v = to_num(r.iloc[tc])
            except ValueError:
                continue
            totals[cat] = totals.get(cat, 0.0) + v
    return totals


def _eq_shares_from_totals(totals: dict[str, float], month: str) -> dict[str, float]:
    parts = {"share_prop": 0.0, "share_retail": 0.0, "share_foreign": 0.0, "share_local_inst": 0.0}
    missing = [k for k in EQ_BUCKETS if k not in totals]
    if missing:
        raise ValueError(f"categories missing: {missing}")
    for cat, bucket in EQ_BUCKETS.items():
        parts[bucket] += totals[cat]
    s = sum(parts.values())
    if "Total" in totals and totals["Total"] > 0 and abs(s - totals["Total"]) / totals["Total"] > 0.005:
        log.warning("TSE %s: leaf categories sum %.0f != file total %.0f", month, s, totals["Total"])
    return common.shares_from_parts(parts)


def _collect_tse_shares(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    years = {int(m[:4]) for m in months}
    files = _discover(EQ_INVESTOR_INDEX, r"stock_(?:val_1|1)_m(\d{4}|\d{6})\.xlsx?$", years)
    if not files:
        log.error("TSE: no investor-type files discovered")
        return rows
    for month in months:
        url = files.get(_yymm(month)) or files.get(_yyyymm(month))
        if not url:
            log.warning("TSE %s: investor-type file not yet published", month)
            continue
        try:
            data = _fetch(url)
            xl = pd.ExcelFile(io.BytesIO(data))
            totals: dict[str, float] = {}
            if url.lower().endswith(".xls"):
                for sh in EQ_TSE_SHEETS:
                    if sh not in xl.sheet_names:
                        raise ValueError(f"sheet {sh!r} missing in {url}")
                    for k, v in _eq_old_sheet_totals(xl.parse(sh, header=None)).items():
                        totals[k] = totals.get(k, 0.0) + v
            else:
                totals = _eq_new_sheet_totals(xl.parse(xl.sheet_names[0], header=None))
            shares = _eq_shares_from_totals(totals, month)
        except Exception as e:  # noqa: BLE001
            log.warning("TSE %s: investor-type parse failed (%s): %s", month, url, e)
            continue
        for metric, v in shares.items():
            rows.append(row(month=month, country=COUNTRY, exchange="TSE", segment="stocks", product="ALL",
                            metric=metric, basis="value", value=v, source=url))
    return rows


# ----------------------------------------------------------------------------
# OSE: volumes / values from tv_ts time series (fallback: soukatsu_M.xlsx)
def _parse_tv_ts(data: bytes) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (per-month aggregates, per-month per-product dict).

    agg[month] = {days, total_vol, total_val, fut_vol, fut_val, opt_vol, opt_val}
    prod[month][code] = {segment, vol, val}
    """
    xl = pd.ExcelFile(io.BytesIO(data))
    sheet = next((s for s in xl.sheet_names if "monthly" in s.lower() or "月間" in s), xl.sheet_names[0])
    df = xl.parse(sheet, header=None)
    hdr = None
    for i in range(min(20, len(df))):
        v = df.iloc[i, 0]
        if pd.notna(v) and "year" in str(v).lower():
            hdr = i
            break
    if hdr is None:
        raise ValueError("tv_ts header row not found")
    # column -> English product name (text after the last line break)
    names: dict[int, str] = {}
    for c, v in enumerate(df.iloc[hdr]):
        if pd.notna(v) and c >= 2:
            names[c] = _norm(str(v).split("\n")[-1])
    sub_rx = re.compile(r"^(取引高|取引金額)")
    # each product spans two columns: volume at c, value at c+1
    cols: dict[int, tuple[str, str, str]] = {}  # vol col -> (kind, name, code)
    for c, nm in names.items():
        if nm in ("total",) or "related" in nm:
            continue
        if "【うち" in str(df.iloc[hdr, c]):  # "of which" sub-breakdowns of Securities Options
            continue
        kind = "options" if "option" in nm else "futures"
        code = DV_VOLUME_PRODUCTS.get(nm, (None, None))[1]
        cols[c] = (kind, nm, code)
    total_col = next((c for c, nm in names.items() if nm == "total"), 2)

    agg: dict[str, dict] = {}
    prod: dict[str, dict] = {}
    for i in range(hdr + 1, len(df)):
        d = df.iloc[i, 0]
        if pd.isna(d):
            continue
        try:
            ts = pd.Timestamp(d)
        except Exception:  # noqa: BLE001
            continue
        month = f"{ts.year:04d}-{ts.month:02d}"
        try:
            days = int(to_num(df.iloc[i, 1]))
        except ValueError:
            days = None
        a = {"days": days, "fut_vol": 0.0, "fut_val": 0.0, "opt_vol": 0.0, "opt_val": 0.0}
        a["total_vol"] = _num0(df.iloc[i, total_col]) or None
        a["total_val"] = _num0(df.iloc[i, total_col + 1]) or None
        p: dict[str, dict] = {}
        for c, (kind, nm, code) in cols.items():
            vol = _num0(df.iloc[i, c])
            val = _num0(df.iloc[i, c + 1])
            a[f"{'fut' if kind == 'futures' else 'opt'}_vol"] += vol
            a[f"{'fut' if kind == 'futures' else 'opt'}_val"] += val
            if code:
                p[code] = {"segment": kind, "vol": vol, "val": val}
        agg[month] = a
        prod[month] = p
    return agg, prod


def _parse_soukatsu(data: bytes) -> tuple[dict[str, dict], dict[str, dict]]:
    """Fallback: soukatsu_M.xlsx (per-product rows, no grand totals)."""
    df = pd.read_excel(io.BytesIO(data), header=None)
    hdr = None
    for i in range(min(10, len(df))):
        if any(pd.notna(v) and str(v).strip() == "Year" for v in df.iloc[i]):
            hdr = i
            break
    if hdr is None:
        raise ValueError("soukatsu header not found")
    h = [(_norm(v) if pd.notna(v) else "") for v in df.iloc[hdr]]
    c_year, c_month, c_days = h.index("year"), h.index("month"), next(i for i, v in enumerate(h) if v.startswith("trading"))
    c_vol = next(i for i, v in enumerate(h) if v.startswith("total(unit"))
    c_val = next(i for i, v in enumerate(h) if v.startswith("total(") and "unit" not in v)
    agg: dict[str, dict] = {}
    prod: dict[str, dict] = {}
    for i in range(hdr + 1, len(df)):
        r = df.iloc[i]
        nm = _norm(r.iloc[1]) if pd.notna(r.iloc[1]) else ""
        pc = str(r.iloc[3]).strip() if pd.notna(r.iloc[3]) else ""
        if not nm or pc in ("put", "call", "Put", "Call"):
            continue
        try:
            month = f"{int(to_num(r.iloc[c_year])):04d}-{int(to_num(r.iloc[c_month])):02d}"
        except ValueError:
            continue
        kind = "options" if "option" in nm else "futures"
        vol = _num0(r.iloc[c_vol])
        val = _num0(r.iloc[c_val])
        a = agg.setdefault(month, {"days": None, "fut_vol": 0.0, "fut_val": 0.0, "opt_vol": 0.0, "opt_val": 0.0,
                                   "total_vol": None, "total_val": None})
        try:
            a["days"] = int(to_num(r.iloc[c_days]))
        except ValueError:
            pass
        a[f"{'fut' if kind == 'futures' else 'opt'}_vol"] += vol
        a[f"{'fut' if kind == 'futures' else 'opt'}_val"] += val
        code = DV_VOLUME_PRODUCTS.get(nm, DV_VOLUME_PRODUCTS.get(nm.replace("mini-topix", "mini topix"), (None, None)))[1]
        if code:
            prod.setdefault(month, {})[code] = {"segment": kind, "vol": vol, "val": val}
    return agg, prod


def _collect_ose_volume(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    years = {int(m[:4]) for m in months}
    files = _discover(DV_SUMMARY_INDEX, r"tv_ts(\d{6})\.xlsx?$", years)
    agg = prod = None
    src = None
    if files:
        src = files[max(files)]
        try:
            agg, prod = _parse_tv_ts(_fetch(src))
        except Exception as e:  # noqa: BLE001
            log.warning("OSE: tv_ts parse failed (%s): %s", src, e)
            agg = None
    if agg is None:
        src = DV_SUMMARY_FALLBACK
        try:
            agg, prod = _parse_soukatsu(_fetch(src, ttl=INDEX_TTL_SEC))
        except Exception as e:  # noqa: BLE001
            log.error("OSE: fallback soukatsu_M parse failed: %s", e)
            return rows

    for month in months:
        a = agg.get(month)
        if not a:
            log.warning("OSE %s: not in %s", month, src)
            continue
        if a.get("total_vol") is not None:
            calc = a["fut_vol"] + a["opt_vol"]
            if a["total_vol"] and abs(calc - a["total_vol"]) / a["total_vol"] > 0.001:
                log.warning("OSE %s: futures+options volume %.0f != file total %.0f", month, calc, a["total_vol"])
        for seg, vk, xk, metric in (("futures", "fut_vol", "fut_val", "turnover_notional"),
                                    ("options", "opt_vol", "opt_val", "turnover_premium")):
            rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=seg, product="ALL",
                            metric="contracts", value=a[vk], source=src))
            rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=seg, product="ALL",
                            metric=metric, value=a[xk], currency=CCY, source=src))
            if a.get("days"):
                rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=seg, product="ALL",
                                metric="trading_days", value=a["days"], source=src))
        for code, d in sorted((prod.get(month) or {}).items()):
            metric = "turnover_notional" if d["segment"] == "futures" else "turnover_premium"
            rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=d["segment"], product=code,
                            metric="contracts", value=d["vol"], source=src))
            if d["val"]:
                rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=d["segment"], product=code,
                                metric=metric, value=d["val"], currency=CCY, source=src))
    return rows


# ----------------------------------------------------------------------------
# OSE: investor-type shares from Tousi_DV_M CSV
def _parse_tousi_csv(data: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("cannot decode CSV")
    first = text.lstrip().split("\n", 1)[0]
    if re.match(r'^\s*"?\d{3}"?\s*,', first):
        # Layout used for transactions before 2026-04-13 (guide_20250526.xls): no header,
        # 24 columns: ptype, vol-unit, val-unit, cycle, yyyymmw, from, to, investor,
        # sales(vol, ratio, bal), sales(value, ratio, bal), purchases(vol...), purchases(value...),
        # total vol, ratio, total value, ratio. Investor codes include aggregates
        # 70 brokerage / 80 grand / 30 institutions / 20 financial institutions, and
        # Proprietary / Individuals are "10" / "50" (they are "11" / "51" in the new layout).
        raw = pd.read_csv(io.StringIO(text), header=None, dtype=str)
        if raw.shape[1] < 23:
            raise ValueError(f"unexpected old-layout width {raw.shape[1]}")
        # old leaf codes: 10 Proprietary, 50 Individuals (new layout: 11 and 51)
        inv = raw[7].astype(str).str.strip().replace({"10": "11", "50": "51"})
        # Columns 1/2 give per-PRODUCT unit exponents (volume: 0 -> 1 unit, 2 -> 100, 3 -> 1000;
        # value: 0 -> yen, 2 -> 100 yen, 3 -> 1,000 yen, 4 -> 10,000 yen, 8 -> 100m yen). They differ by
        # product (e.g. 301 Nikkei 225 Futures in thousand yen, 317 JGB Futures in 10,000 yen), so
        # everything is rescaled to base units here; otherwise cross-product sums (product ALL) are wrong.
        parts = []
        for kind, col, ucol in (("1", 20, 1), ("2", 22, 2)):
            unit = pd.to_numeric(raw[ucol], errors="coerce").fillna(0).astype(int)
            tot = pd.to_numeric(raw[col], errors="coerce").fillna(0.0) * (10.0 ** unit)
            parts.append(pd.DataFrame({"ptype": raw[0].astype(str).str.strip(), "inv": inv, "kind": kind,
                                       "total": tot}))
        return pd.concat(parts, ignore_index=True)
    df = pd.read_csv(io.StringIO(text), dtype=str)
    df.columns = [str(c) for c in df.columns]
    # positional: 0 product type, 1 cycle, 2 yyyymmw, 3 from, 4 to, 5 investor code, 6 vol/val flag, 7.. sales.., 11 total
    df = df.rename(columns={df.columns[0]: "ptype", df.columns[5]: "inv", df.columns[6]: "kind",
                            df.columns[len(df.columns) - 1]: "total"})
    df["ptype"] = df["ptype"].astype(str).str.strip()
    df["inv"] = df["inv"].astype(str).str.strip()
    df["kind"] = df["kind"].astype(str).str.strip()
    df["total"] = pd.to_numeric(df["total"].astype(str).str.replace(",", ""), errors="coerce").fillna(0.0)
    return df


def _dv_shares(df: pd.DataFrame, codes: list[str], kind: str) -> dict[str, float] | None:
    sub = df[df["ptype"].isin(codes) & (df["kind"] == kind)]
    if sub.empty:
        return None
    parts = {"share_prop": 0.0, "share_retail": 0.0, "share_foreign": 0.0, "share_local_inst": 0.0}
    unknown = set()
    for inv, tot in zip(sub["inv"], sub["total"]):
        b = DV_INVESTOR_BUCKET.get(inv)
        if b is None:
            if inv not in DV_AGGREGATE_CODES:
                unknown.add(inv)
            continue
        parts[b] += float(tot)
    if unknown:
        log.warning("OSE investor-type: unknown investor codes %s ignored", sorted(unknown))
    try:
        return common.shares_from_parts(parts)
    except ValueError:
        return None


def _collect_ose_shares(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    years = {int(m[:4]) for m in months}
    files = _discover(DV_INVESTOR_INDEX, r"Tousi_DV_M_(\d{6})[^/\"]*\.csv$", years)
    if not files:
        log.error("OSE: no Tousi_DV_M CSVs discovered")
        return rows
    for month in months:
        url = files.get(_yyyymm(month))
        if not url:
            log.warning("OSE %s: investor-type CSV not yet published", month)
            continue
        try:
            df = _parse_tousi_csv(_fetch(url))
        except Exception as e:  # noqa: BLE001
            log.warning("OSE %s: investor-type CSV parse failed (%s): %s", month, url, e)
            continue
        all_codes = set(df["ptype"].unique())
        targets = dict(DV_INVESTOR_PRODUCTS)
        targets["ALL:futures"] = ("futures", sorted(all_codes - DV_OPTION_CODES))
        targets["ALL:options"] = ("options", sorted(all_codes & DV_OPTION_CODES))
        for key, (seg, codes) in targets.items():
            product = "ALL" if key.startswith("ALL:") else key
            # value basis for everything; contracts basis only for single products (contracts
            # are not additive across products with different multipliers)
            bases = [("2", "value")] if product == "ALL" else [("1", "contracts"), ("2", "value")]
            for flag, basis in bases:
                shares = _dv_shares(df, codes, flag)
                if not shares:
                    log.info("OSE %s: no investor data for %s (%s)", month, product, basis)
                    continue
                for metric, v in shares.items():
                    rows.append(row(month=month, country=COUNTRY, exchange="OSE", segment=seg, product=product,
                                    metric=metric, basis=basis, value=v, source=url))
    return rows


# ----------------------------------------------------------------------------
def collect(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    for name, fn in (("TSE turnover", _collect_tse_turnover), ("TSE shares", _collect_tse_shares),
                     ("OSE volume", _collect_ose_volume), ("OSE shares", _collect_ose_shares)):
        try:
            got = fn(months)
            log.info("%s: %d rows", name, len(got))
            rows.extend(got)
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed: %s", name, e)
    return rows


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ms = sys.argv[1:] or common.last_n_months(12)
    out = collect(ms)
    pd.DataFrame(out).to_csv(sys.stdout, index=False)
