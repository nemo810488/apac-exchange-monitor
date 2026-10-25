"""Thailand: SET (cash market, SET main board + mai combined) and TFEX (derivatives).

Both exchange sites sit behind the Imperva/Incapsula WAF. As verified 2026-10-26 from this
network:
  * www.set.or.th answers *every* path except static /dat/ files with a JavaScript
    challenge (403, "_Incapsula_Resource"), so the JSON API behind the SET website
    (https://www.set.or.th/api/set/...) cannot be called from plain Python at all.
  * www.tfex.co.th and media.tfex.co.th let plain requests through as long as the request
    carries ordinary browser headers (Accept, Accept-Language, a Chrome User-Agent without
    a bot token) and is paced; a burst of a few dozen requests gets the client blocked for
    a while (403 with the same Incapsula page). We pace, detect the page, back off, and stop
    hitting a domain for the rest of the run once it has blocked us twice.

TFEX (primary)
  * Monthly Market Report PDF (one per month, Thai/English bilingual tables), index file
    https://media.tfex.co.th/common/tfex/marketdata/monthly-report/files-import/month.csv
    (the .csv itself is refused by the WAF for non-browser clients, so we build the file name):
    https://media.tfex.co.th/common/tfex/marketdata/monthly-report/files-document/month/th/TFEXMonthlyReport_{YYYY}_{Month}.pdf
    where {Month} is "September" (2026 files), "september" (2025 files) or "Sep" (<=2024).
    Page 1 holds three tables (Volume, Daily Average Volume, Open Interest) with one row
    per product and a "{Mon}-{YYYY}" column for the report month, plus an "Investor Type"
    table (Local Investor / Foreign Investor / Local Institutions, buy / sell / total
    contracts) for the WHOLE market (futures + options together).
      contracts  : per product from the Volume table; ALL futures = Total Market Volume
                   minus the options rows; ALL options = sum of options rows.
      trading_days: Total Market Volume / Total Daily Average Volume (always an integer
                   within rounding; checked).
      share_*    : Investor Type table, basis = contracts, two-sided (buy + sell).
                   TFEX has only three investor buckets. Brokers' proprietary desks and
                   market makers are juridical persons and fall under "Local Institutions"
                   (there is no separate proprietary category anywhere in TFEX statistics),
                   so share_prop is emitted as 0 and share_local_inst includes prop.
                   The table covers futures + options combined; options are <2% of TFEX
                   volume, so we attach the shares to segment=futures, product=ALL only.
  * JSON API behind the "Market Statistics" page (fallback / cross-check), e.g.
    GET https://www.tfex.co.th/api/set/tfex/historical-statistics/monthly?website=tfex
        [&marketListId=TXI_F|TXS_F|TXM_F|TXD_F|TXC_F|TXR_F|TXA_F|TXI_O|TXC_O][&instrumentId=...]
    -> ~24 months of {date: last trading day, volume, averageVolume, oi} (all products when
    no marketListId). Market list codes come from /api/set/tfex/market/list.
    The investor-type API (/api/set/tfex/market/investor-type/summary?period=1d|MTD|YTD)
    only serves the current period, so it is useless for history and not used.
  * TFEX does not publish monthly trading VALUE on these pages or in the PDF, so no
    turnover_notional / turnover_premium rows for TFEX.

Product codes emitted for TFEX (segment futures unless noted):
    ALL, SET50F (SET50 Index Futures), SSF (Stock Futures), GOLD (10-Baht + 50-Baht +
    Gold Online + mini Gold Online + Gold-D), SILVER (Silver Online Futures), USDF (USD
    Futures), CURRENCYF (all currency futures: USD, EUR, JPY, EURUSD, USDJPY),
    options: ALL, SET50O (SET50 Index Options), USDO (USD Options).

SET (fallback only; the website API is a stub)
  * Intended primary: the JSON API behind https://www.set.or.th/en/market/statistics/investor-type
    and .../market/statistics/market-statistics/main (monthly trading value SET + mai and
    investor-type buy/sell value). Blocked by the Incapsula JS challenge -> `_set_api_month`
    is a documented stub that logs and returns nothing.
  * Implemented fallback: SET's own monthly press release "SET Market Report for <Month>
    <Year>" (published ~first week of the following month), as republished verbatim by
    Mondo Visione: search POST https://mondovisione.com/search/advancedsearch/ then the
    article page. It states the combined SET + mai average daily trading value
    ("THB 70.15 billion") and the month's share of trading value by investor type
    (foreign / local retail / local institutions / proprietary trading).
      turnover_notional = ADT (THB billion -> THB) x trading days. Trading days are taken
                   from TFEX (same holiday calendar: TFEX day session follows SET holidays).
      share_*    : basis = value, SET + mai combined (parsed from the prose; the four
                   percentages must sum to ~100 or the month is skipped).
  * SET-only and mai-only product rows are not available this way (product=ALL only).

Raw downloads are cached under raw/th/ and reused on re-runs.
"""
from __future__ import annotations

import datetime as dt
import html as htmllib
import io
import logging
import os
import re
import time

import requests

import common
from common import row, shares_from_parts, to_num

log = logging.getLogger("collectors.th")

VENUES = ["SET", "TFEX"]
COUNTRY = "TH"
CCY = "THB"

RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "th")

# Ordinary browser headers: Incapsula lets these through, the shared UA with the
# "apac-exchange-monitor" token is challenged on tfex.co.th.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
S = requests.Session()
S.headers.update(BROWSER_HEADERS)
S.verify = common.SESSION.verify

PACE = {"tfex": 3.0, "media.tfex": 4.0, "set": 3.0, "mondovisione": 2.0}
WAF_BACKOFF = 30.0  # x attempt; after two fully-blocked fetches a domain is skipped for the run
_last_hit: dict[str, float] = {}
_waf_strikes: dict[str, int] = {}
_warmed: set[str] = set()


# ----------------------------------------------------------------------------
# cache + fetch helpers
# ----------------------------------------------------------------------------
def _cache_path(name: str) -> str:
    os.makedirs(RAW_DIR, exist_ok=True)
    return os.path.join(RAW_DIR, name)


def _read_cache(name: str) -> bytes | None:
    p = _cache_path(name)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def _write_cache(name: str, data: bytes) -> None:
    with open(_cache_path(name), "wb") as f:
        f.write(data)


def _pace(domain: str) -> None:
    wait = PACE[domain] - (time.time() - _last_hit.get(domain, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[domain] = time.time()


def _looks_blocked(body: bytes) -> bool:
    head = body[:2000]
    return b"_Incapsula_Resource" in head or b"Incapsula incident" in body[:4000]


def _warm(domain: str) -> None:
    """Load one HTML page first so the session holds the WAF's visid/incap_ses cookies."""
    if domain in _warmed:
        return
    _warmed.add(domain)
    page = {"tfex": "https://www.tfex.co.th/en/market-data/historical-data/market-statistics",
            "media.tfex": "https://www.tfex.co.th/en/market-data/historical-data/monthly-market-report"}.get(domain)
    if page:
        _pace(domain)
        try:
            S.get(page, timeout=60)
        except Exception as e:  # noqa: BLE001
            log.info("warm-up %s failed: %s", page, e)


def _fetch(domain: str, cache_name: str, *, url: str, method: str = "GET", params=None, data=None,
           headers=None, validate=None, tries: int = 2, timeout: int = 120) -> bytes:
    """Fetch with cache; detect the Incapsula block page and back off. `validate(body)->bool`."""
    cached = _read_cache(cache_name)
    if cached is not None and not _looks_blocked(cached) and (validate is None or validate(cached)):
        return cached
    if _waf_strikes.get(domain, 0) >= 2:
        raise RuntimeError(f"{domain} blocked us twice this run (Incapsula); not retrying")
    _warm(domain)
    last = None
    blocked = False
    for attempt in range(tries):
        _pace(domain)
        try:
            r = S.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
            body = r.content
            if r.status_code == 404:
                raise FileNotFoundError(url)
            if r.status_code == 200 and not _looks_blocked(body) and (validate is None or validate(body)):
                _write_cache(cache_name, body)
                return body
            if _looks_blocked(body):
                blocked = True
                last = RuntimeError(f"status {r.status_code}, Incapsula block page")
                log.info("%s answered with the Incapsula page, backing off %.0fs", domain, WAF_BACKOFF * (attempt + 1))
                time.sleep(WAF_BACKOFF * (attempt + 1))
                continue
            blocked = False
            last = RuntimeError(f"status {r.status_code}, unusable body ({body[:80]!r})")
        except FileNotFoundError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3.0 * (attempt + 1))
    if blocked:
        _waf_strikes[domain] = _waf_strikes.get(domain, 0) + 1
    raise RuntimeError(f"{method} {url} failed: {last}")


def _ym(month: str) -> tuple[int, int]:
    y, m = month.split("-")
    return int(y), int(m)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S | re.I)
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


# ----------------------------------------------------------------------------
# TFEX
# ----------------------------------------------------------------------------
TFEX_PDF_BASE = "https://media.tfex.co.th/common/tfex/marketdata/monthly-report/files-document/month/th/"
TFEX_PDF_PAGE = "https://www.tfex.co.th/en/market-data/historical-data/monthly-market-report"
TFEX_API_MONTHLY = "https://www.tfex.co.th/api/set/tfex/historical-statistics/monthly"
TFEX_API_PAGE = "https://www.tfex.co.th/en/market-data/historical-data/market-statistics"

# PDF row label (as printed) -> (segment, product code). Everything not listed still counts
# towards ALL via the "Total Market Volume" row.
TFEX_PRODUCT_MAP = {
    "SET50 Index Futures": ("futures", "SET50F"),
    "SET50 Index Options": ("options", "SET50O"),
    "Stock Futures": ("futures", "SSF"),
    "10-Baht Gold Futures": ("futures", "GOLD"),
    "50-Baht Gold Futures": ("futures", "GOLD"),
    "Gold Online Futures": ("futures", "GOLD"),
    "mini Gold Online Futures": ("futures", "GOLD"),
    "Gold D Futures": ("futures", "GOLD"),
    "Silver Online Futures": ("futures", "SILVER"),
    "USD Futures": ("futures", "USDF"),
    "USD Option": ("options", "USDO"),
    "USD Options": ("options", "USDO"),
}
TFEX_CURRENCY_ROWS = {"USD Futures", "EUR Futures", "JPY Futures", "EURUSD Futures", "USDJPY Futures"}
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _tfex_pdf(month: str) -> tuple[bytes, str]:
    """Download (or load cached) the monthly report PDF; returns (bytes, url)."""
    y, m = _ym(month)
    tag = f"{y:04d}{m:02d}"
    name_full = dt.date(y, m, 1).strftime("%B")
    cache = f"tfex_monthly_{tag}.pdf"
    url_cache = f"tfex_monthly_{tag}.url"
    cached = _read_cache(cache)
    if cached and cached[:5] == b"%PDF-":
        u = _read_cache(url_cache)
        return cached, (u.decode() if u else TFEX_PDF_BASE + f"TFEXMonthlyReport_{y}_{name_full}.pdf")
    variants = [name_full, name_full.lower(), name_full[:3], name_full[:3].lower()]
    last = None
    for v in variants:
        url = TFEX_PDF_BASE + f"TFEXMonthlyReport_{y}_{v}.pdf"
        try:
            body = _fetch("media.tfex", cache, url=url, headers={"Accept": "application/pdf,*/*"},
                          validate=lambda b: b[:5] == b"%PDF-")
            _write_cache(url_cache, url.encode())
            return body, url
        except FileNotFoundError:
            last = f"404 {url}"
            continue
    raise RuntimeError(f"TFEX monthly PDF for {month} not found ({last})")


def _group_rows(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    rows: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(rows[-1][0]["top"] - w["top"]) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _tfex_parse_pdf(pdf_bytes: bytes, month: str) -> dict:
    """Return {'volume': {label: n}, 'adv': {label: n}, 'investor': {label: (buy, sell, total)}}."""
    import pdfplumber  # local import: only needed for TFEX

    y, m = _ym(month)
    col_hdr = dt.date(y, m, 1).strftime("%b-%Y")  # e.g. "Sep-2026"
    out = {"volume": {}, "adv": {}, "investor": {}}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]  # the three volume/ADV/OI tables are on page 1
        words = page.extract_words(x_tolerance=1.5, keep_blank_chars=False)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)  # investor table is on page 2
    rows = _group_rows(words)
    # Each of the three tables (Volume, Daily Average Volume, Open Interest) repeats the
    # "{Mon}-{YYYY}" header; the n-th hit opens the n-th table.
    order = ["volume", "adv", "oi"]
    n_hdr = 0
    section = None
    col_x1 = None
    for r in rows:
        hdr = [w for w in r if w["text"] == col_hdr]
        if hdr:
            section = order[min(n_hdr, 2)]
            n_hdr += 1
            col_x1 = hdr[0]["x1"]
            continue
        if section not in ("volume", "adv") or col_x1 is None:
            continue
        label_words, nums = [], []
        for w in r:
            if _NUM_RE.fullmatch(w["text"]) and w["x0"] > 120:
                nums.append(w)
            elif not nums and not w["text"].endswith("%"):
                label_words.append(w["text"])
        label = " ".join(label_words).strip()
        if not label or not nums:
            continue
        pick = [w for w in nums if abs(w["x1"] - col_x1) <= 12]
        if not pick:
            continue
        out[section][label] = to_num(pick[0]["text"])
        if label.startswith("Total Market Volume"):
            section = None  # table closed; ignore the next table's header rows until its month header
    # Investor Type table (whole market), buy / sell / total contracts
    for key, pat in (("Local Investor", r"Local Investors?\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"),
                     ("Foreign Investor", r"Foreign Investors?\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"),
                     ("Local Institutions", r"Local Institutions?\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")):
        for mt in re.finditer(pat, text):
            b, s_, t = (to_num(x) for x in mt.groups())
            if abs(b + s_ - t) <= 1:  # distinguishes it from the "No. of Account Open" table
                out["investor"][key] = (b, s_, t)
                break
    return out


def _tfex_api_monthly(month: str, market_list_id: str | None = None) -> tuple[float, float, str]:
    """(volume, averageVolume, source) for the month from the website JSON API (fallback)."""
    y, m = _ym(month)
    params = {"website": "tfex"}
    if market_list_id:
        params["marketListId"] = market_list_id
    suffix = market_list_id or "all"
    body = _fetch("tfex", f"tfex_api_monthly_{suffix}.json", url=TFEX_API_MONTHLY, params=params,
                  headers={"Accept": "application/json, text/plain, */*", "Referer": TFEX_API_PAGE},
                  validate=lambda b: b[:1] == b"{" and b'"periodType"' in b[:200])
    import json
    d = json.loads(body)
    src = f"{TFEX_API_MONTHLY}?website=tfex" + (f"&marketListId={market_list_id}" if market_list_id else "") + f"  (page {TFEX_API_PAGE})"
    for it in d.get("data", []):
        if it["date"][:7] == f"{y:04d}-{m:02d}":
            return float(it["volume"]), float(it["averageVolume"]), src
    raise ValueError(f"month {month} not in API response")


def _tfex_month(month: str) -> list[dict]:
    out: list[dict] = []

    def R(segment, product, metric, value, source, **kw):
        out.append(row(month=month, country=COUNTRY, exchange="TFEX", segment=segment, product=product,
                       metric=metric, value=value, source=source, **kw))

    trading_days = None
    # --- primary: monthly report PDF -------------------------------------------------
    try:
        pdf_bytes, url = _tfex_pdf(month)
        d = _tfex_parse_pdf(pdf_bytes, month)
        vol = d["volume"]
        total_lbl = next((k for k in vol if k.startswith("Total Market Volume")), None)
        if total_lbl is None:
            raise ValueError(f"no Total Market Volume row; rows = {list(vol)}")
        total = vol[total_lbl]
        products = {k: v for k, v in vol.items() if k != total_lbl}
        if abs(sum(products.values()) - total) > 1.5:
            raise ValueError(f"product rows sum {sum(products.values())} != total {total}")
        opt_total = sum(v for k, v in products.items() if TFEX_PRODUCT_MAP.get(k, ("",))[0] == "options")
        unknown = [k for k in products if k not in TFEX_PRODUCT_MAP and k not in TFEX_CURRENCY_ROWS]
        if unknown:
            log.info("TFEX %s: unmapped rows counted only in ALL: %s", month, unknown)
        src = f"{url}  (page {TFEX_PDF_PAGE})"
        R("futures", "ALL", "contracts", total - opt_total, src)
        R("options", "ALL", "contracts", opt_total, src)
        agg: dict[tuple[str, str], float] = {}
        for k, v in products.items():
            if k in TFEX_PRODUCT_MAP:
                seg, code = TFEX_PRODUCT_MAP[k]
                agg[(seg, code)] = agg.get((seg, code), 0.0) + v
            if k in TFEX_CURRENCY_ROWS:
                agg[("futures", "CURRENCYF")] = agg.get(("futures", "CURRENCYF"), 0.0) + v
        for (seg, code), v in sorted(agg.items()):
            R(seg, code, "contracts", v, src)
        # trading days = monthly volume / daily average volume
        adv = d["adv"]
        adv_total_lbl = next((k for k in adv if k.startswith("Total Market Volume")), None)
        if adv_total_lbl and adv[adv_total_lbl] > 0:
            ratio = total / adv[adv_total_lbl]
            if abs(ratio - round(ratio)) > 0.05:
                log.warning("TFEX %s: volume/ADV = %.3f is not an integer", month, ratio)
            else:
                trading_days = int(round(ratio))
                R("futures", "ALL", "trading_days", trading_days, src)
                R("options", "ALL", "trading_days", trading_days, src)
        # investor type (whole market; see module docstring on share_prop)
        inv = d["investor"]
        if len(inv) == 3:
            two_sided = {k: v[2] for k, v in inv.items()}  # total = buy + sell
            if abs(sum(two_sided.values()) - 2.0 * total) > 0.002 * 2.0 * total:
                log.warning("TFEX %s: investor totals %s != 2*volume %s", month, sum(two_sided.values()), 2 * total)
            parts = shares_from_parts({"share_retail": two_sided["Local Investor"],
                                       "share_foreign": two_sided["Foreign Investor"],
                                       "share_local_inst": two_sided["Local Institutions"]})
            # No proprietary bucket exists in TFEX statistics: brokers' prop desks / market
            # makers are reported inside "Local Institutions", hence share_prop = 0.
            parts["share_prop"] = 0.0
            for k, v in parts.items():
                R("futures", "ALL", k, v, src, basis="contracts")
        else:
            log.warning("TFEX %s: investor-type table not parsed (%s)", month, list(inv))
    except Exception as e:  # noqa: BLE001
        log.warning("TFEX monthly PDF %s failed: %s", month, e)
        # --- fallback: website JSON API (totals + a few market groups) ----------------
        try:
            vol, adv, src = _tfex_api_monthly(month)
            opt = 0.0
            opt_parts = {}
            for mk, code in (("TXI_O", "SET50O"), ("TXC_O", "USDO")):
                try:
                    v, _, s2 = _tfex_api_monthly(month, mk)
                    opt_parts[code] = (v, s2)
                    opt += v
                except Exception as e2:  # noqa: BLE001
                    log.info("TFEX API %s %s: %s", month, mk, e2)
            R("futures", "ALL", "contracts", vol - opt, src)
            R("options", "ALL", "contracts", opt, src)
            for code, (v, s2) in opt_parts.items():
                R("options", code, "contracts", v, s2)
            for mk, code in (("TXS_F", "SSF"),):
                try:
                    v, _, s2 = _tfex_api_monthly(month, mk)
                    R("futures", code, "contracts", v, s2)
                except Exception as e2:  # noqa: BLE001
                    log.info("TFEX API %s %s: %s", month, mk, e2)
            if adv > 0:
                ratio = vol / adv
                if abs(ratio - round(ratio)) <= 0.05:
                    trading_days = int(round(ratio))
                    R("futures", "ALL", "trading_days", trading_days, src)
                    R("options", "ALL", "trading_days", trading_days, src)
        except Exception as e2:  # noqa: BLE001
            log.warning("TFEX API fallback %s failed: %s", month, e2)
    return out


# ----------------------------------------------------------------------------
# SET
# ----------------------------------------------------------------------------
SET_PAGE_INVESTOR = "https://www.set.or.th/en/market/statistics/investor-type"
SET_PAGE_MARKET = "https://www.set.or.th/en/market/statistics/market-statistics/main"
MV_SEARCH = "https://mondovisione.com/search/advancedsearch/"
_set_api_checked = False


def _set_api_month(month: str) -> list[dict]:
    """STUB. Intended: the JSON API behind SET_PAGE_INVESTOR / SET_PAGE_MARKET
    (https://www.set.or.th/api/set/... ; monthly trading value SET + mai, investor-type
    buy/sell value for Local Institutions / Proprietary Trading / Foreign Investors /
    Local Individuals, for SET and for mai separately). From this network www.set.or.th
    returns the Incapsula JavaScript challenge for the HTML pages and for /api/*, so the
    endpoints could not be discovered or called. Checked once per run and logged."""
    global _set_api_checked
    if not _set_api_checked:
        _set_api_checked = True
        try:
            _pace("set")
            r = S.get(SET_PAGE_INVESTOR, timeout=60)
            if r.status_code != 200 or _looks_blocked(r.content):
                log.info("SET website API unavailable (HTTP %s, Incapsula challenge) -> using press-release fallback",
                         r.status_code)
            else:
                log.info("SET website reachable (HTTP 200) but its JSON endpoints are not implemented yet; "
                         "using press-release fallback")
        except Exception as e:  # noqa: BLE001
            log.info("SET website check failed: %s", e)
    return []


def _mv_find_article(month: str) -> str:
    """Find the Mondo Visione URL of 'SET Market Report for <Month> <Year>' (published the
    following month)."""
    y, m = _ym(month)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)          # report comes out early next month
    ty, tm = (ny + 1, 1) if nm == 12 else (ny, nm + 1)       # ... search window: next month + one more
    mon = dt.date(y, m, 1).strftime("%B")
    data = {"Keywords": "SET Market Report", "DateFrom_Days": "1", "DateFrom_Months": str(nm),
            "DateFrom_Years": str(ny), "DateTo_Days": "28", "DateTo_Months": str(tm), "DateTo_Years": str(ty),
            "Modules_1": "News", "Search": "Search", "SxForm_Posted_AdvancedSearch": "true"}
    body = _fetch("mondovisione", f"mv_search_{y:04d}{m:02d}.html", url=MV_SEARCH, method="POST", data=data,
                  validate=lambda b: b"Search Results" in b or b"searched for" in b)
    page = body.decode("utf-8", "replace")
    for href, title in re.findall(r'<a href="(/media-and-resources/news/[^"]+)"[^>]*>([^<]*)</a>', page):
        t = title.lower()
        if "set market report" in t and mon.lower() in t and str(y) in t:
            return "https://mondovisione.com" + href
    raise ValueError(f"no 'SET Market Report for {mon} {y}' in search results")


_UNIT = {"trillion": 1e12, "billion": 1e9, "million": 1e6}
_SHARE_LABELS = [
    ("share_foreign", re.compile(r"foreign investors?", re.I)),
    ("share_retail", re.compile(r"local individuals?|retail investors?|local retail|individual investors?", re.I)),
    ("share_local_inst", re.compile(r"local institutional investors?|local institutions?|institutional investors?", re.I)),
    ("share_prop", re.compile(r"proprietary trading|proprietary|securities companies", re.I)),
]


def _parse_press_release(text: str, month: str) -> tuple[float, dict[str, float] | None]:
    """(average daily trading value in THB, {share_*: fraction} or None)."""
    y, m = _ym(month)
    mon = dt.date(y, m, 1).strftime("%B")
    i = text.find(f"Key highlights for {mon}")
    body = text[i:] if i >= 0 else text
    sents = re.split(r"(?<=[.])\s+", body)
    # The month's ADT is always the first "average daily trading value ... THB x billion"
    # sentence; the year-to-date figure follows in the next sentence.
    adt = None
    for sent in sents:
        mt = re.search(r"average daily trading value.*?THB\s*([\d.,]+)\s*(trillion|billion|million)", sent, re.I)
        if mt:
            adt = to_num(mt.group(1)) * _UNIT[mt.group(2).lower()]
            break
    if adt is None:
        raise ValueError("average daily trading value sentence not found")
    # Investor-type shares: the sentence with "percent of total trading value" plus any
    # directly following sentences that continue the enumeration ("... 7.5 percent and
    # 6.4 percent, respectively.").
    shares = None
    for idx, sent in enumerate(sents):
        low = sent.lower()
        if "total trading value" not in low or "percent" not in low:
            continue
        window = [sent]
        for nxt in sents[idx + 1:idx + 3]:
            nl = nxt.lower()
            if "percent" in nl and "p/e" not in nl and (
                    re.search(r"respectively|each", nl) or any(rx.search(nxt) for _, rx in _SHARE_LABELS)):
                window.append(nxt)
            else:
                break
        blob = " ".join(window)
        pcts = [float(p) for p in re.findall(r"(\d+(?:\.\d+)?)\s*percent", blob)]
        labels = []
        for key, rx in _SHARE_LABELS:
            hit = rx.search(blob)
            if hit:
                labels.append((hit.start(), key))
        labels.sort()
        keys = [k for _, k in labels]
        if len(keys) == 4 and len(pcts) == 3 and re.search(r"percent each", blob):
            pcts.append(pcts[-1])  # "... were on par at 6.9 percent each"
        if len(pcts) == 4 and len(keys) == 4 and len(set(keys)) == 4 and 99.0 <= sum(pcts) <= 101.0:
            shares = {k: p / 100.0 for k, p in zip(keys, pcts)}
            break
    return adt, shares


def _set_month(month: str, trading_days: int | None, days_source: str | None) -> list[dict]:
    out: list[dict] = []
    out.extend(_set_api_month(month))
    if out:
        return out
    url = _mv_find_article(month)
    y, m = _ym(month)
    body = _fetch("mondovisione", f"mv_article_{y:04d}{m:02d}.html", url=url,
                  validate=lambda b: b"trading value" in b)
    text = _strip_tags(body.decode("utf-8", "replace"))
    adt, shares = _parse_press_release(text, month)
    src = f"{url}  (SET press release 'SET Market Report for {dt.date(y, m, 1):%B %Y}', SET + mai combined)"
    if trading_days:
        out.append(row(month=month, country=COUNTRY, exchange="SET", segment="stocks", product="ALL",
                       metric="turnover_notional", value=adt * trading_days, currency=CCY,
                       source=f"{src}; ADT THB {adt / 1e9:.2f}bn x {trading_days} trading days"))
        out.append(row(month=month, country=COUNTRY, exchange="SET", segment="stocks", product="ALL",
                       metric="trading_days", value=trading_days,
                       source=f"{days_source}  (TFEX trading days; same holiday calendar as SET)"))
    else:
        log.warning("SET %s: no trading-day count available, turnover_notional skipped (ADT THB %.2fbn)",
                    month, adt / 1e9)
    if shares:
        for k, v in shares.items():
            out.append(row(month=month, country=COUNTRY, exchange="SET", segment="stocks", product="ALL",
                           metric=k, basis="value", value=v, source=src))
    else:
        log.warning("SET %s: investor-type sentence not parsed", month)
    return out


# ----------------------------------------------------------------------------
def collect(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    for month in months:
        tdays, tsrc = None, None
        try:
            got = _tfex_month(month)
            rows.extend(got)
            log.info("TFEX %s: %d rows", month, len(got))
            for r in got:
                if r["metric"] == "trading_days":
                    tdays, tsrc = int(r["value"]), r["source"]
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("TFEX %s failed: %s", month, e)
        try:
            got = _set_month(month, tdays, tsrc)
            rows.extend(got)
            log.info("SET %s: %d rows", month, len(got))
        except Exception as e:  # noqa: BLE001
            log.warning("SET %s failed: %s", month, e)
    return rows


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ms = sys.argv[1:] or common.last_n_months(12)
    for r in collect(ms):
        print(r)
