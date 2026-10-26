"""Hong Kong: HKEX securities market (Main Board + GEM) and HKEX derivatives market.

VOLUMES ONLY. HKEX publishes no monthly participant / investor-type breakdown (only the
biennial Cash Market Transaction Survey), so this module emits no share_* rows by design.

Verified 2026-10-26 from this network: www.hkex.com.hk answers plain `requests` with 200
(no JS challenge) as long as requests are paced. The statistics pages are Sitecore pages
whose tables are either rendered server-side (Monthly Market Highlights) or loaded by the
page's JavaScript from static JSON files under /eng/stat/ (everything else). We read those
JSON files directly.

Securities market (segment stocks)
  * ALL (Main Board + GEM) -- HKEX Monthly Market Highlights, one HTML page per month:
      https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/HKEX-Monthly-Market-Highlights?sc_lang=en&select={GUID}
    The month -> {GUID} map is read from the month dropdown of the index page (same URL
    without `select`; it lists the latest 13 months, older months are not reachable here).
    Table "Securities Market Turnover (Main Board and GEM)" gives "Monthly turnover ($Mil.)"
    (-> turnover_notional, HK$ million x 1e6 = HKD; includes securities traded in non-HKD
    currencies, converted by HKEX) and "No. of trading days" (-> trading_days).
    Table "Stock Connect Summary" gives Northbound average daily turnover (RMB Mil) and
    Southbound average daily turnover ($ Mil) plus the NB / SB trading-day counts:
      NB turnover_notional = NB ADT x NB trading days  (currency CNY, x 1e6)
      SB turnover_notional = SB ADT x SB trading days  (currency HKD, x 1e6)
    (HKEX only publishes the Connect ADT on this page, so the monthly total is derived.)
  * MAIN / GEM -- Monthly Bulletin "Stock market highlights" JSON (static, one per month):
      https://www.hkex.com.hk/eng/stat/smstat/mthbull/rpt_Stock_market_highlights_{YYMM}.json      (Main Board)
      https://www.hkex.com.hk/eng/stat/smstat/mthbull/rpt_Stock_market_highlights_GEM_{YYMM}.json  (GEM)
    (manifests: .../MonthlyBulletin_{YYMM}.json and .../MonthlyBulletin_GEM_{YYMM}.json, behind
    https://www.hkex.com.hk/Market-Data/Statistics/Consolidated-Reports/Monthly-Bulletin?sc_lang=en
    and .../Monthly-Bulletin-GEM). Row "Turnover value (HK$mil) - Daily average" holds
    "{month total}<br/>{daily average}"; month total x 1e6 -> turnover_notional (HKD).
    MAIN + GEM reconciles exactly to the Highlights total (checked for Sep 2026).
    If the Highlights page is unavailable for a month, ALL falls back to MAIN + GEM and
    trading_days to round(total / daily average).

Derivatives market (segments futures / options)
  * Monthly Statistics page https://www.hkex.com.hk/Market-Data/Statistics/Derivatives-Market/Monthly-Statistics?sc_lang=en
    loads the manifest https://www.hkex.com.hk/eng/stat/dmstat/marksum/MonthlyStatistics_en.json
    (label -> JSON url for "Futures and Options" + ~100 individual products). Each JSON holds a
    rolling 13-month table (rows "YYYY MM", the current month as "YYYY MM (up to MM DD)"):
      MonthlyStatistics_FnO.json           : Futures / Options / F&O  Contract Volume + Open Interest
      MonthlyStatistics_F_{code}_{id}.json : No. of Trading Days, Average Daily, Total, Open Interest
      MonthlyStatistics_O_{code}_{id}.json : No. of Trading Days, Average Daily, Call, Put, Total, OI x3
    Product files are looked up by *label* (ids are stable but labels are what the page shows).
      contracts   : ALL futures / ALL options = FnO.json; products below from their own files.
      trading_days: "No. of Trading Days" of the Hang Seng Index Futures file (HKEX counts
                    regular trading days, holiday-trading days excluded), attached to both segments.
      OTHER       : ALL minus the sum of the emitted products, per segment, so components
                    reconcile to the totals.
    HKEX publishes only contract volume and open interest in these monthly statistics (no
    contract value / notional and no option premium), so no turnover_notional or
    turnover_premium rows are produced for the derivatives market.

Product codes emitted (segment futures): ALL, HSIF (Hang Seng Index Futures), HSIMF (Mini
HSI Futures), HHIF (HSCEI Futures), HHIMF (Mini HSCEI Futures), HTIF (Hang Seng TECH Index
Futures), SF (Stock Futures), USDCNH (RMB Currency Futures - USD/CNH, standard contract only;
the mini contract is in OTHER), OTHER.
Segment options: ALL, HSIO (HSI Options + Weekly HSI Options + Flexible HSI Options), HSIFO
(HSI Futures Options), HSIMO (Mini HSI Options), HHIO (HSCEI Options + Weekly + Flexible),
HHIFO (HSCEI Futures Options), HHIMO (Mini HSCEI Options), HTIO (Hang Seng TECH Index Options
+ Weekly), HTIFO (HSTECH Futures Options), SO (Stock Options), OTHER.

Raw downloads are cached under raw/hk/ (HTML per month, bulletin JSON per month, the rolling
derivatives JSONs per calendar day) and reused on re-runs.
"""
from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import logging
import os
import re
import time

import common
from common import row, to_num

log = logging.getLogger("collectors.hk")

VENUES = ["HKEX"]
COUNTRY = "HK"
CCY = "HKD"

RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "hk")

BASE = "https://www.hkex.com.hk"
MMH_PAGE = BASE + "/Market-Data/Statistics/Consolidated-Reports/HKEX-Monthly-Market-Highlights?sc_lang=en"
MB_PAGE = BASE + "/Market-Data/Statistics/Consolidated-Reports/Monthly-Bulletin?sc_lang=en"
MB_GEM_PAGE = BASE + "/Market-Data/Statistics/Consolidated-Reports/Monthly-Bulletin-GEM?sc_lang=en"
MB_JSON = BASE + "/eng/stat/smstat/mthbull/rpt_Stock_market_highlights_{yymm}.json"
MB_GEM_JSON = BASE + "/eng/stat/smstat/mthbull/rpt_Stock_market_highlights_GEM_{yymm}.json"
DM_PAGE = BASE + "/Market-Data/Statistics/Derivatives-Market/Monthly-Statistics?sc_lang=en"
DM_MANIFEST = BASE + "/eng/stat/dmstat/marksum/MonthlyStatistics_en.json"

S = common.SESSION
JSON_HEADERS = {"Accept": "application/json, text/plain, */*", "Referer": BASE + "/"}
HTML_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

PACE_SECONDS = 2.5
WAF_BACKOFF = 30.0  # x attempt
_last_hit = 0.0
_waf_strikes = 0

# Derivatives product label (as in the manifest) -> (segment, product code)
DM_PRODUCTS = {
    "Hang Seng Index Futures": ("futures", "HSIF"),
    "Mini Hang Seng Index Futures": ("futures", "HSIMF"),
    "Hang Seng China Enterprises Index Futures": ("futures", "HHIF"),
    "Mini-Hang Seng China Enterprises Index Futures": ("futures", "HHIMF"),
    "Hang Seng TECH Index Futures": ("futures", "HTIF"),
    "Stock Futures": ("futures", "SF"),
    "RMB Currency Futures - USD/CNH Futures": ("futures", "USDCNH"),
    "Hang Seng Index Options": ("options", "HSIO"),
    "Weekly Hang Seng Index Options": ("options", "HSIO"),
    "Flexible Hang Seng Index Options": ("options", "HSIO"),
    "Hang Seng Index Futures Options": ("options", "HSIFO"),
    "Mini Hang Seng Index Options": ("options", "HSIMO"),
    "Hang Seng China Enterprises Index Options": ("options", "HHIO"),
    "Weekly Hang Seng China Enterprises Index Options": ("options", "HHIO"),
    "Flexible Hang Seng China Enterprises Index Options": ("options", "HHIO"),
    "Hang Seng China Enterprises Index Futures Options": ("options", "HHIFO"),
    "Mini-Hang Seng China Enterprises Index Options": ("options", "HHIMO"),
    "Hang Seng TECH Index Options": ("options", "HTIO"),
    "Weekly Hang Seng TECH Index Options": ("options", "HTIO"),
    "Hang Seng TECH Index Futures Options": ("options", "HTIFO"),
    "Stock Options": ("options", "SO"),
}
DM_TRADING_DAYS_LABEL = "Hang Seng Index Futures"


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


def _pace() -> None:
    global _last_hit
    wait = PACE_SECONDS - (time.time() - _last_hit)
    if wait > 0:
        time.sleep(wait)
    _last_hit = time.time()


def _looks_blocked(body: bytes, status: int) -> bool:
    """HKEX sits behind a CDN/WAF; a challenge or deny page is short HTML without our tables."""
    head = body[:4000].lower()
    if status in (401, 403, 429, 503):
        return True
    markers = (b"access denied", b"request unsuccessful", b"_incapsula_resource", b"captcha",
               b"cf-browser-verification", b"attention required", b"edgesuite.net")
    return any(m in head for m in markers)


def _fetch(cache_name: str, url: str, *, headers=None, validate=None, tries: int = 2,
           timeout: int = 90) -> bytes:
    """GET with cache, pacing and WAF detection. `validate(body) -> bool` guards the cache."""
    global _waf_strikes
    cached = _read_cache(cache_name)
    if cached is not None and (validate is None or validate(cached)):
        return cached
    if _waf_strikes >= 2:
        raise RuntimeError("hkex.com.hk blocked us twice this run; not retrying")
    last = None
    blocked = False
    for attempt in range(tries):
        _pace()
        try:
            r = S.get(url, headers=headers, timeout=timeout)
            body = r.content
            if r.status_code == 404:
                raise FileNotFoundError(url)
            if _looks_blocked(body, r.status_code):
                blocked = True
                last = RuntimeError(f"status {r.status_code}, WAF/deny page")
                log.info("hkex answered %s with a block page, backing off %.0fs", r.status_code,
                         WAF_BACKOFF * (attempt + 1))
                time.sleep(WAF_BACKOFF * (attempt + 1))
                continue
            blocked = False
            if r.status_code == 200 and (validate is None or validate(body)):
                _write_cache(cache_name, body)
                return body
            last = RuntimeError(f"status {r.status_code}, unusable body ({body[:80]!r})")
        except FileNotFoundError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(3.0 * (attempt + 1))
    if blocked:
        _waf_strikes += 1
    raise RuntimeError(f"GET {url} failed: {last}")


def _ym(month: str) -> tuple[int, int]:
    y, m = month.split("-")
    return int(y), int(m)


def _yymm(month: str) -> str:
    y, m = _ym(month)
    return f"{y % 100:02d}{m:02d}"


def _clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = htmllib.unescape(re.sub(r"<[^>]+>", " ", s)).replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


# ----------------------------------------------------------------------------
# HKEX JSON table format (shared by bulletin + derivatives statistics)
# ----------------------------------------------------------------------------
def _json_tables(body: bytes) -> list[tuple[list[list[str]], list[list[str]]]]:
    """Return [(header_rows, body_rows)] with cell texts, from HKEX's {tables:[{header,body}]}."""
    d = json.loads(body.decode("utf-8-sig", "replace"))
    out = []
    for t in d.get("tables", []):
        hdr: dict[int, list[tuple[int, str]]] = {}
        for c in t.get("header", []):
            hdr.setdefault(c["row"], []).append((c["col"], _clean(c.get("text", ""))))
        bod: dict[int, list[tuple[int, str]]] = {}
        for c in t.get("body", []):
            bod.setdefault(c["row"], []).append((c["col"], _clean(c.get("text", ""))))
        out.append(([[x for _, x in sorted(v)] for _, v in sorted(hdr.items())],
                    [[x for _, x in sorted(v)] for _, v in sorted(bod.items())]))
    return out


def _is_json_table(body: bytes) -> bool:
    return body.lstrip()[:1] in (b"{", b"[") and b'"tables"' in body[:500]


# ----------------------------------------------------------------------------
# Securities market: Monthly Market Highlights (HTML, one page per month)
# ----------------------------------------------------------------------------
_mmh_index: dict[str, str] | None = None  # "YYYY-MM" -> {GUID}


def _mmh_month_guids() -> dict[str, str]:
    """Month -> Sitecore item GUID from the Highlights dropdown (latest 13 months)."""
    global _mmh_index
    if _mmh_index is not None:
        return _mmh_index
    today = dt.date.today().strftime("%Y%m%d")
    body = _fetch(f"mmh_index_{today}.html", MMH_PAGE, headers=HTML_HEADERS,
                  validate=lambda b: b"select-target-firstdropdownlist" in b)
    page = body.decode("utf-8", "replace")
    idx: dict[str, str] = {}
    for guid, title in re.findall(r'data-id="(\{[0-9A-Fa-f-]+\})"[^>]*>\s*<a[^>]*title="([^"]+)"', page):
        try:
            d = dt.datetime.strptime(title.strip(), "%B %Y")
        except ValueError:
            continue
        idx.setdefault(f"{d.year:04d}-{d.month:02d}", guid)
    # the currently selected month sits in the select-target, not in the item list
    m = re.search(r'id="select-target-firstdropdownlist"[^>]*data-id="(\{[0-9A-Fa-f-]+\})"[^>]*title="([^"]+)"', page)
    if m:
        try:
            d = dt.datetime.strptime(m.group(2).strip(), "%B %Y")
            idx.setdefault(f"{d.year:04d}-{d.month:02d}", m.group(1))
        except ValueError:
            pass
    if not idx:
        raise RuntimeError("Monthly Market Highlights: month dropdown not found")
    _mmh_index = idx
    return idx


def _html_tables(page: str) -> list[list[list[str]]]:
    """All <table>s of the page as lists of rows of cleaned cell texts."""
    tables = []
    for tb in re.findall(r"<table.*?</table>", page, flags=re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tb, flags=re.S | re.I):
            cells = [_clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _mmh_month(month: str) -> dict:
    """Parse one Highlights page. Returns {turnover_hkd, trading_days, nb_adt_rmb, nb_days, sb_adt_hkd, sb_days, url}."""
    y, m = _ym(month)
    guids = _mmh_month_guids()
    if month not in guids:
        raise FileNotFoundError(f"{month} not in the Highlights dropdown (available: {min(guids)}..{max(guids)})")
    url = f"{MMH_PAGE}&select={guids[month]}"
    want_long = dt.date(y, m, 1).strftime("%B %Y")
    want_short = dt.date(y, m, 1).strftime("%b %Y")

    def ok(b: bytes) -> bool:
        s = b.decode("utf-8", "replace")
        sel = re.search(r'id="select-target-firstdropdownlist"[^>]*title="([^"]+)"', s)
        return bool(sel) and sel.group(1).strip() == want_long and "Securities Market Turnover" in s

    body = _fetch(f"mmh_{y:04d}{m:02d}.html", url, headers=HTML_HEADERS, validate=ok)
    page = body.decode("utf-8", "replace")
    out: dict = {"url": url}

    def first_col_value(label_rx: str, header_rx: str, table_title: str) -> str:
        """Value in the column headed by this month, from the table whose row label matches."""
        for rows in _html_tables(page):
            hdr = rows[0]
            col = next((i for i, h in enumerate(hdr) if re.search(header_rx, h)), None)
            if col is None:
                continue
            for r in rows[1:]:
                if r and re.search(label_rx, r[0], flags=re.I):
                    if col < len(r):
                        return r[col]
        raise ValueError(f"{table_title}: row /{label_rx}/ for {want_short} not found")

    hdr_rx = r"^" + re.escape(want_short) + r"\b"
    out["turnover_hkd"] = to_num(first_col_value(r"^Monthly turnover \(\$ ?Mil", hdr_rx, "Securities Market Turnover").split("\n")[0]) * 1e6
    out["trading_days"] = int(to_num(first_col_value(r"^No\. of trading days", hdr_rx, "Securities Market Turnover")))
    adt = to_num(first_col_value(r"^Average daily turnover by value", hdr_rx, "Securities Market Turnover")) * 1e6
    if abs(out["turnover_hkd"] / adt - out["trading_days"]) > 0.02:
        log.warning("HKEX %s: turnover / ADT = %.3f but trading days = %d", month,
                    out["turnover_hkd"] / adt, out["trading_days"])
    # Stock Connect (optional)
    try:
        out["nb_adt_rmb"] = to_num(first_col_value(r"^Total Northbound average daily turnover", hdr_rx, "Stock Connect")) * 1e6
        out["nb_days"] = int(to_num(first_col_value(r"^No\. of Northbound trading days", hdr_rx, "Stock Connect")))
        out["sb_adt_hkd"] = to_num(first_col_value(r"^Total Southbound average daily turnover", hdr_rx, "Stock Connect")) * 1e6
        out["sb_days"] = int(to_num(first_col_value(r"^No\. of Southbound trading days", hdr_rx, "Stock Connect")))
    except Exception as e:  # noqa: BLE001
        log.info("HKEX %s: Stock Connect table not parsed: %s", month, e)
    return out


# ----------------------------------------------------------------------------
# Securities market: Monthly Bulletin JSON (Main Board / GEM)
# ----------------------------------------------------------------------------
def _bulletin(month: str, gem: bool) -> tuple[float, float, str]:
    """(month turnover HKD, daily average HKD, url) from the 'Stock market highlights' JSON."""
    y, m = _ym(month)
    url = (MB_GEM_JSON if gem else MB_JSON).format(yymm=_yymm(month))
    body = _fetch(f"mb_{'gem' if gem else 'main'}_{y:04d}{m:02d}.json", url, headers=JSON_HEADERS,
                  validate=_is_json_table)
    want = dt.date(y, m, 1).strftime("%B %Y")
    for hdr, rows in _json_tables(body):
        if not hdr or len(hdr[0]) < 2 or not hdr[0][1].startswith(want):
            raise ValueError(f"bulletin header {hdr[:1]} does not start with {want!r}")
        for r in rows:
            if r and r[0].startswith("Turnover value (HK$mil)"):
                parts = [p for p in r[1].split("\n") if p.strip()]
                if len(parts) < 2:
                    raise ValueError(f"unexpected turnover cell {r[1]!r}")
                return to_num(parts[0]) * 1e6, to_num(parts[1]) * 1e6, url
    raise ValueError("Turnover value row not found")


# ----------------------------------------------------------------------------
# Derivatives market: Monthly Statistics JSON
# ----------------------------------------------------------------------------
_dm_manifest: dict[str, str] | None = None
_dm_cache: dict[str, tuple[dict[str, list[str]], list[str], str]] = {}


def _dm_files() -> dict[str, str]:
    global _dm_manifest
    if _dm_manifest is None:
        today = dt.date.today().strftime("%Y%m%d")
        body = _fetch(f"dm_manifest_{today}.json", DM_MANIFEST, headers=JSON_HEADERS,
                      validate=lambda b: b.lstrip()[:1] == b"[" and b"MonthlyStatistics_FnO" in b)
        _dm_manifest = {it["label"].strip(): it["url"] for it in json.loads(body.decode("utf-8-sig", "replace"))}
    return _dm_manifest


def _dm_table(label: str) -> tuple[dict[str, list[str]], list[str], str]:
    """{ 'YYYY-MM': [cells...] }, flattened header, url for one Monthly Statistics file."""
    if label in _dm_cache:
        return _dm_cache[label]
    files = _dm_files()
    if label not in files:
        raise KeyError(f"{label!r} not in the Monthly Statistics manifest")
    url = BASE + files[label]
    today = dt.date.today().strftime("%Y%m%d")
    fname = os.path.basename(files[label]).replace(".json", "")
    body = _fetch(f"dm_{fname}_{today}.json", url, headers=JSON_HEADERS, validate=_is_json_table)
    tables = _json_tables(body)
    if not tables:
        raise ValueError(f"no table in {url}")
    hdr_rows, rows = tables[0]
    # flatten the 2-row header: top-level labels with colspan are expanded in the JSON by col index
    flat = [" / ".join(x for x in col if x) for col in zip(*hdr_rows)] if len(hdr_rows) > 1 and \
        len({len(h) for h in hdr_rows}) == 1 else [c for h in hdr_rows for c in h]
    by_month: dict[str, list[str]] = {}
    for r in rows:
        mt = re.match(r"^(\d{4})\s+(\d{2})(\s*\(.*\))?$", r[0])
        if not mt:
            continue
        if mt.group(3):  # "(up to MM DD)" -> partial month, skip
            continue
        by_month[f"{mt.group(1)}-{mt.group(2)}"] = r
    _dm_cache[label] = (by_month, flat, url)
    return _dm_cache[label]


def _dm_month(month: str) -> list[dict]:
    out: list[dict] = []

    def R(segment, product, metric, value, source, **kw):
        out.append(row(month=month, country=COUNTRY, exchange="HKEX", segment=segment, product=product,
                       metric=metric, value=value, source=source, **kw))

    src_page = f"  (page {DM_PAGE})"
    # totals
    by_month, hdr, url = _dm_table("Futures and Options")
    if month not in by_month:
        raise FileNotFoundError(f"{month} not in Futures and Options statistics (have {min(by_month)}..{max(by_month)})")
    r = by_month[month]
    # columns: Month | Futures vol | Futures OI | Options vol | Options OI | F&O vol | F&O OI
    fut_total, opt_total, fno_total = to_num(r[1]), to_num(r[3]), to_num(r[5])
    if abs(fut_total + opt_total - fno_total) > 0.5:
        raise ValueError(f"futures {fut_total} + options {opt_total} != F&O {fno_total}")
    R("futures", "ALL", "contracts", fut_total, url + src_page)
    R("options", "ALL", "contracts", opt_total, url + src_page)

    # trading days (regular trading days, from the HSI futures file)
    try:
        bm, _, u = _dm_table(DM_TRADING_DAYS_LABEL)
        if month in bm:
            tdays = int(to_num(bm[month][1]))
            R("futures", "ALL", "trading_days", tdays, u + src_page)
            R("options", "ALL", "trading_days", tdays, u + src_page)
    except Exception as e:  # noqa: BLE001
        log.warning("HKEX derivatives %s: trading days not available: %s", month, e)

    # products
    agg: dict[tuple[str, str], float] = {}
    srcs: dict[tuple[str, str], list[str]] = {}
    for label, (seg, code) in DM_PRODUCTS.items():
        try:
            bm, h, u = _dm_table(label)
        except Exception as e:  # noqa: BLE001
            log.info("HKEX derivatives %s: %s skipped: %s", month, label, e)
            continue
        if month not in bm:
            log.info("HKEX derivatives %s: %s has no row for this month", month, label)
            continue
        rr = bm[month]
        # futures: Month | days | ADV | Total | OI ; options: Month | days | ADV | Call | Put | Total | OI...
        vol = to_num(rr[5]) if seg == "options" else to_num(rr[3])
        if seg == "options" and abs(to_num(rr[3]) + to_num(rr[4]) - vol) > 0.5:
            log.warning("HKEX %s %s: call %s + put %s != total %s", month, label, rr[3], rr[4], rr[5])
        agg[(seg, code)] = agg.get((seg, code), 0.0) + vol
        srcs.setdefault((seg, code), []).append(u)
    for (seg, code), v in sorted(agg.items()):
        R(seg, code, "contracts", v, "; ".join(dict.fromkeys(srcs[(seg, code)])) + src_page)
    for seg, total in (("futures", fut_total), ("options", opt_total)):
        parts = sum(v for (s, _), v in agg.items() if s == seg)
        if parts:
            other = total - parts
            if other < -0.5:
                log.warning("HKEX %s %s: components %s exceed total %s", month, seg, parts, total)
            R(seg, "OTHER", "contracts", max(other, 0.0), url + src_page)
    return out


# ----------------------------------------------------------------------------
# Securities month
# ----------------------------------------------------------------------------
def _sec_month(month: str) -> list[dict]:
    out: list[dict] = []

    def R(product, metric, value, source, currency=""):
        out.append(row(month=month, country=COUNTRY, exchange="HKEX", segment="stocks", product=product,
                       metric=metric, value=value, source=source, currency=currency))

    main = gem = None
    for gem_flag, code in ((False, "MAIN"), (True, "GEM")):
        try:
            total, adt, url = _bulletin(month, gem_flag)
            R(code, "turnover_notional", total, f"{url}  (HK$ million x 1e6; page {MB_GEM_PAGE if gem_flag else MB_PAGE})", CCY)
            if gem_flag:
                gem = (total, adt)
            else:
                main = (total, adt)
        except Exception as e:  # noqa: BLE001
            log.warning("HKEX %s: Monthly Bulletin %s failed: %s", month, code, e)

    try:
        d = _mmh_month(month)
        src = f"{d['url']}  (Monthly Market Highlights, Main Board + GEM, HK$ million x 1e6)"
        R("ALL", "turnover_notional", d["turnover_hkd"], src, CCY)
        R("ALL", "trading_days", d["trading_days"], src)
        if main and gem and abs(main[0] + gem[0] - d["turnover_hkd"]) > 1e6:
            log.warning("HKEX %s: MAIN %.0f + GEM %.0f != ALL %.0f (HKD)", month, main[0], gem[0], d["turnover_hkd"])
        if "nb_adt_rmb" in d:
            R("NB", "turnover_notional", d["nb_adt_rmb"] * d["nb_days"],
              f"{d['url']}  (Stock Connect Northbound ADT RMB million x 1e6 x {d['nb_days']} NB trading days)", "CNY")
            R("NB", "trading_days", d["nb_days"], d["url"] + "  (Stock Connect Northbound trading days)")
            R("SB", "turnover_notional", d["sb_adt_hkd"] * d["sb_days"],
              f"{d['url']}  (Stock Connect Southbound ADT HK$ million x 1e6 x {d['sb_days']} SB trading days)", CCY)
            R("SB", "trading_days", d["sb_days"], d["url"] + "  (Stock Connect Southbound trading days)")
    except Exception as e:  # noqa: BLE001
        log.warning("HKEX %s: Monthly Market Highlights failed (%s); ALL from MAIN + GEM", month, e)
        if main and gem:
            total = main[0] + gem[0]
            src = f"{MB_JSON.format(yymm=_yymm(month))}; {MB_GEM_JSON.format(yymm=_yymm(month))}  (Main Board + GEM, HK$ million x 1e6)"
            R("ALL", "turnover_notional", total, src, CCY)
            ratio = main[0] / main[1] if main[1] else 0
            if ratio and abs(ratio - round(ratio)) < 0.05:
                R("ALL", "trading_days", int(round(ratio)), src + "; trading days = Main Board turnover / daily average")
            else:
                log.warning("HKEX %s: trading days not derivable (turnover/ADT = %.3f)", month, ratio)
    return out


# ----------------------------------------------------------------------------
def collect(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    for month in months:
        try:
            got = _sec_month(month)
            rows.extend(got)
            log.info("HKEX securities %s: %d rows", month, len(got))
        except Exception as e:  # noqa: BLE001
            log.warning("HKEX securities %s failed: %s", month, e)
        try:
            got = _dm_month(month)
            rows.extend(got)
            log.info("HKEX derivatives %s: %d rows", month, len(got))
        except Exception as e:  # noqa: BLE001
            log.warning("HKEX derivatives %s failed: %s", month, e)
    return rows


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ms = sys.argv[1:] or common.last_n_months(12)
    for r in collect(ms):
        print(r)
