"""Taiwan: TWSE (cash, main board), TPEx (cash, OTC) and TAIFEX (derivatives).

Sources (all public, no auth), as verified 2026-10-23:

TWSE
  * FMTQIK  GET https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date=YYYYMM01&response=json
            Daily market summary for the month. hints = "單位：元、股" -> trade value in TWD
            base units, volume in shares. One row per trading day.
            NOTE: the English twin (/rwd/en/...) is answered by the TWSE WAF with a 307
            "FOR SECURITY REASONS" page from this network; the zh endpoint returns the
            same numbers. The WAF also rate-limits bursts (same 307 page) -> we pace
            requests and retry on a non-JSON body.
  * BFI82U  GET https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&type=month&monthDate=YYYYMM01
            "三大法人買賣金額統計表" for the month, unit TWD. Rows: dealers (prop),
            dealers (hedge), investment trust, foreign & mainland (ex foreign dealers),
            foreign dealers, total; columns buy / sell / net.

TPEx (new site: pattern /www/{LANG}/{ACTION}, POST form fields, response=json)
  * POST https://www.tpex.org.tw/www/en-us/afterTrading/marketStats  type=Monthly&date=YYYY/MM/01
            Monthly trading value (NT$), volume (shares), transactions by security type
            (general stocks, altered-trading stocks, warrants, ETFs, convertible bonds, ...).
            No total row; we sum the categories.
  * POST https://www.tpex.org.tw/www/en-us/insti/summary  type=Monthly&date=YYYY/MM/01&prod=0
            Foreign & institutional investors monthly buy/sell amounts (NTD), securities
            (= everything except convertible bonds / bonds with warrants).
  * POST https://www.tpex.org.tw/www/en-us/indexInfo/inx  date=YYYY/MM/01
            Daily TPEx index history for the month -> trading-day count.

TAIFEX
  * POST https://www.taifex.com.tw/enl/eng7/tradingVolume  syear=YYYY&smonth=M
            "Monthly Trading Statistics – by Product Category and Participant": total
            contracts and buy/sell contracts by participant type (individuals, securities
            dealers, investment trusts, foreign institutions, managed futures/futures trust,
            other institutions, FCM proprietary) per product category and for all
            futures / all options.
  * POST https://www.taifex.com.tw/enl/eng7/monthlyFutVolData  queryMonth=YYYYMM
            Monthly contracts, Single Stock Futures vs ETF Futures.
  * POST https://www.taifex.com.tw/enl/eng7/contractMonthDataDown  queryMonth=YYYYMM&queryTypeMonth=FUT|OPT
            CSV, monthly trading value per product in *millions of TWD*. FUT: notional.
            OPT: notional and premium. (Data available from 2025-01.)
  * POST https://www.taifex.com.tw/enl/eng3/futDataDown  (and .../optDataDown for options)
            down_type=1&commodity_id=TX&queryStartDate=YYYY/MM/01&queryEndDate=YYYY/MM/DD
            Daily per-contract CSV (cp950) for one product, max one month per request.
            Volume per row (Regular + After-Hours sessions). Calendar-spread rows
            ("202609/202612") must be excluded: TAIFEX's official product total
            (checked against the Daily Market Report subtotal) does not count them.
            commodity_id=all returns HTTP 520 for a full month, so we download per product.

Raw downloads are cached under raw/tw/ and reused on re-runs.
"""
from __future__ import annotations

import csv
import gzip
import html as htmllib
import io
import json
import logging
import os
import re
import time

import common
from common import month_bounds, row, shares_from_parts, to_num

log = logging.getLogger("collectors.tw")

VENUES = ["TWSE", "TPEX", "TAIFEX"]
COUNTRY = "TW"
CCY = "TWD"

RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raw", "tw")

# Polite pacing (seconds) between live requests per domain. TWSE's WAF is the touchiest.
PACE = {"twse": 6.0, "tpex": 1.5, "taifex": 2.0}
# When a WAF "for security reasons" page comes back, wait this long (x attempt) before retrying.
WAF_BACKOFF = 30.0
_last_hit: dict[str, float] = {}

# TAIFEX product codes we report individually.
TAIFEX_FUT_PRODUCTS = ["TX", "MTX", "TMF"]   # TAIEX futures, mini, micro
TAIFEX_OPT_PRODUCTS = ["TXO"]                # TAIEX options
# STF (single stock futures) comes from the monthly tables, not from the daily download.


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
        data = f.read()
    if p.endswith(".gz"):
        data = gzip.decompress(data)
    return data


def _write_cache(name: str, data: bytes) -> None:
    p = _cache_path(name)
    if p.endswith(".gz"):
        data = gzip.compress(data)
    with open(p, "wb") as f:
        f.write(data)


def _pace(domain: str) -> None:
    wait = PACE[domain] - (time.time() - _last_hit.get(domain, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[domain] = time.time()


def _looks_blocked(body: bytes) -> bool:
    head = body[:600].lower()
    return (b"for security reasons" in head) or head.strip().startswith(b"error code:")


def _fetch(domain: str, cache_name: str, *, url: str, method: str = "GET", params=None, data=None,
           validate=None, tries: int | None = None, timeout: int = 120) -> bytes:
    """Fetch with cache; retry on WAF/5xx pages. `validate(body) -> bool` guards the cache."""
    cached = _read_cache(cache_name)
    if cached is not None and not _looks_blocked(cached) and (validate is None or validate(cached)):
        return cached
    if tries is None:
        tries = 6 if domain == "twse" else 4  # TWSE WAF blocks can last several minutes
    last = None
    for attempt in range(tries):
        _pace(domain)
        try:
            if method == "GET":
                r = common.get(url, params=params, retries=1, timeout=timeout)
            else:
                r = common.post(url, data=data, retries=1, timeout=timeout)
            body = r.content
            if r.status_code == 200 and not _looks_blocked(body) and (validate is None or validate(body)):
                _write_cache(cache_name, body)
                return body
            last = RuntimeError(f"status {r.status_code}, unusable body ({body[:80]!r})")
            if _looks_blocked(body):
                # TWSE's WAF blocks the client for a while after a burst; wait it out.
                log.info("%s answered with a WAF/error page, backing off %.0fs", domain, WAF_BACKOFF * (attempt + 1))
                time.sleep(WAF_BACKOFF * (attempt + 1))
                continue
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(f"{method} {url} failed: {last}")


def _is_json_ok(body: bytes) -> bool:
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return False
    return str(d.get("stat", "")).lower() == "ok"


def _strip_tags(s: str) -> str:
    return htmllib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def _html_tables(page: str) -> list[list[list[str]]]:
    """Return [[cells...] per row] per <table> in the page."""
    out = []
    for t in re.findall(r"<table[^>]*>.*?</table>", page, flags=re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t, flags=re.S | re.I):
            cells = [_strip_tags(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.S | re.I)]
            if cells:
                rows.append(cells)
        out.append(rows)
    return out


def _ym(month: str) -> tuple[int, int]:
    y, m = month.split("-")
    return int(y), int(m)


# ----------------------------------------------------------------------------
# TWSE
# ----------------------------------------------------------------------------
TWSE_FMTQIK = "https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK"
TWSE_BFI82U = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"


def _twse_month(month: str) -> list[dict]:
    y, m = _ym(month)
    out: list[dict] = []
    datestr = f"{y:04d}{m:02d}01"

    # --- total value + trading days -------------------------------------------------
    url = f"{TWSE_FMTQIK}?date={datestr}&response=json"
    body = _fetch("twse", f"twse_FMTQIK_{y:04d}{m:02d}.json", url=url, validate=_is_json_ok)
    d = json.loads(body)
    # fields: 日期, 成交股數, 成交金額, 成交筆數, 發行量加權股價指數, 漲跌點數 ; hints "單位：元、股"
    if "元" not in d.get("hints", "元"):
        log.warning("TWSE FMTQIK %s: unexpected units hint %r", month, d.get("hints"))
    days = [r for r in d["data"] if r and r[0].strip()]
    total_value = sum(to_num(r[2]) for r in days)  # TWD
    n_days = len(days)
    out.append(row(month=month, country=COUNTRY, exchange="TWSE", segment="stocks", product="ALL",
                   metric="turnover_notional", value=total_value, currency=CCY, source=url))
    out.append(row(month=month, country=COUNTRY, exchange="TWSE", segment="stocks", product="ALL",
                   metric="trading_days", value=n_days, source=url))

    # --- investor-type shares ------------------------------------------------------
    # Derivation (basis = value): FMTQIK gives one-sided trading value V. Every trade has a
    # buyer and a seller, so total two-sided activity is 2V. BFI82U gives buy and sell value
    # for the three major institutional investor groups; each group's two-sided activity is
    # buy+sell. Retail is the residual 2V - sum(institutional buy+sell). Caveat: the residual
    # also contains domestic non-financial corporates, insurers, banks and other juridical
    # persons that TWSE does not break out in BFI82U, so share_retail is an upper bound.
    try:
        url2 = f"{TWSE_BFI82U}?response=json&type=month&monthDate={datestr}"
        body2 = _fetch("twse", f"twse_BFI82U_{y:04d}{m:02d}.json", url=url2, validate=_is_json_ok)
        d2 = json.loads(body2)
        if "元" not in d2.get("hints", "元"):  # hints = "單位：元"
            log.warning("TWSE BFI82U %s: unexpected units hint %r", month, d2.get("hints"))
        two_sided = {}
        for r in d2["data"]:
            name = r[0].replace(" ", "")
            two_sided[name] = to_num(r[1]) + to_num(r[2])
        prop = sum(v for k, v in two_sided.items() if k.startswith("自營商"))
        local_inst = sum(v for k, v in two_sided.items() if k.startswith("投信"))
        foreign = sum(v for k, v in two_sided.items() if k.startswith("外資"))
        total2 = 2.0 * total_value
        retail = total2 - prop - local_inst - foreign
        if retail < 0:
            raise ValueError(f"negative residual retail {retail}")
        parts = shares_from_parts({"share_retail": retail, "share_foreign": foreign,
                                   "share_local_inst": local_inst, "share_prop": prop})
        for k, v in parts.items():
            out.append(row(month=month, country=COUNTRY, exchange="TWSE", segment="stocks", product="ALL",
                           metric=k, basis="value", value=v, source=url2))
    except Exception as e:  # noqa: BLE001
        log.warning("TWSE shares %s skipped: %s", month, e)
    return out


# ----------------------------------------------------------------------------
# TPEx
# ----------------------------------------------------------------------------
TPEX_API = "https://www.tpex.org.tw/www/en-us/"
TPEX_PAGE_STATS = "https://www.tpex.org.tw/en-us/mainboard/trading/info/statistics/month.html"
TPEX_PAGE_INSTI = "https://www.tpex.org.tw/en-us/mainboard/trading/major-institutional/summary/month.html"
TPEX_PAGE_INX = "https://www.tpex.org.tw/en-us/indices/stock-index/industrial/inxh.html"


def _tpex_post(action: str, cache_name: str, data: dict) -> dict:
    body = _fetch("tpex", cache_name, url=TPEX_API + action, method="POST",
                  data={**data, "response": "json"}, validate=_is_json_ok)
    return json.loads(body)


def _tpex_month(month: str) -> list[dict]:
    y, m = _ym(month)
    out: list[dict] = []
    date_param = f"{y:04d}/{m:02d}/01"
    tag = f"{y:04d}{m:02d}"

    # --- total value: afterTrading/marketStats, Monthly -----------------------------
    d = _tpex_post("afterTrading/marketStats", f"tpex_marketStats_{tag}.json",
                   {"type": "Monthly", "date": date_param})
    t = d["tables"][0]
    if not t["fields"][1].startswith("Trading Value(NT$)"):
        raise ValueError(f"TPEx marketStats unexpected fields {t['fields']}")
    total_value = 0.0
    value_ex_bonds = 0.0  # denominator for shares (insti/summary excludes CBs/bonds with warrants)
    for r in t["data"]:
        v = to_num(r[1])
        total_value += v
        if "bond" not in r[0].lower():
            value_ex_bonds += v
    src = f"{TPEX_API}afterTrading/marketStats?type=Monthly&date={date_param}  (page {TPEX_PAGE_STATS})"
    out.append(row(month=month, country=COUNTRY, exchange="TPEX", segment="stocks", product="ALL",
                   metric="turnover_notional", value=total_value, currency=CCY, source=src))

    # --- trading days: count of daily TPEx index rows -----------------------------
    try:
        d3 = _tpex_post("indexInfo/inx", f"tpex_inx_{tag}.json", {"date": date_param})
        days = [r for r in d3["tables"][0]["data"] if r and r[0].startswith(f"{y:04d}/{m:02d}")]
        out.append(row(month=month, country=COUNTRY, exchange="TPEX", segment="stocks", product="ALL",
                       metric="trading_days", value=len(days),
                       source=f"{TPEX_API}indexInfo/inx?date={date_param}  (page {TPEX_PAGE_INX})"))
    except Exception as e:  # noqa: BLE001
        log.warning("TPEx trading_days %s skipped: %s", month, e)

    # --- investor-type shares (same residual derivation as TWSE; see _twse_month) -----
    # prod=0 "Securities" = stocks, warrants, ETFs, ETNs, TDRs, beneficiary certs; CBs and bonds
    # with warrants are excluded, so the denominator is marketStats total ex bond categories.
    try:
        d2 = _tpex_post("insti/summary", f"tpex_insti_{tag}.json",
                        {"type": "Monthly", "date": date_param, "prod": "0"})
        t2 = d2["tables"][0]
        if "NTD" not in t2["fields"][1]:
            raise ValueError(f"unexpected fields {t2['fields']}")
        two_sided = {}
        for r in t2["data"]:
            name = r[0].replace("　", "").strip()
            two_sided[name] = to_num(r[1]) + to_num(r[2])
        foreign = (two_sided["Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)"]
                   + two_sided.get("Foreign Dealers", 0.0))
        local_inst = two_sided["Securities Investment Trust Companies"]
        prop = two_sided["Dealers"]  # = proprietary + hedge sub-rows
        retail = 2.0 * value_ex_bonds - foreign - local_inst - prop
        if retail < 0:
            raise ValueError(f"negative residual retail {retail}")
        parts = shares_from_parts({"share_retail": retail, "share_foreign": foreign,
                                   "share_local_inst": local_inst, "share_prop": prop})
        src2 = f"{TPEX_API}insti/summary?type=Monthly&date={date_param}&prod=0  (page {TPEX_PAGE_INSTI})"
        for k, v in parts.items():
            out.append(row(month=month, country=COUNTRY, exchange="TPEX", segment="stocks", product="ALL",
                           metric=k, basis="value", value=v, source=src2))
    except Exception as e:  # noqa: BLE001
        log.warning("TPEx shares %s skipped: %s", month, e)
    return out


# ----------------------------------------------------------------------------
# TAIFEX
# ----------------------------------------------------------------------------
TAIFEX = "https://www.taifex.com.tw/enl/"
TAIFEX_TV = TAIFEX + "eng7/tradingVolume"
TAIFEX_MFV = TAIFEX + "eng7/monthlyFutVolData"
TAIFEX_CTV = TAIFEX + "eng7/contractMonthDataDown"
TAIFEX_CTV_PAGE = TAIFEX + "eng7/contractTradeValue"
TAIFEX_FUT_DL = TAIFEX + "eng3/futDataDown"
TAIFEX_OPT_DL = TAIFEX + "eng3/optDataDown"
TAIFEX_DL_PAGE = TAIFEX + "eng3/futDailyMarketView?menuid1=03"


def _taifex_participant_table(page: str) -> dict[str, dict[str, list[str]]]:
    """Parse eng7/tradingVolume into {'futures': {product: cells}, 'options': {...}}.

    Column layout after the product name: TotalVolume, Individual B/S, SecuritiesDealers B/S,
    InvestmentTrust B/S, Foreign B/S, ManagedFutures B/S, OtherInstitutional B/S,
    Proprietary B/S, MonthEndOI  (16 numeric cells).
    """
    out: dict[str, dict[str, list[str]]] = {"futures": {}, "options": {}}
    section = None
    for table in _html_tables(page):
        if not any(c and c[0] == "Products" for c in table):
            continue
        for cells in table:
            if cells[0] in ("Futures", "Options") and len(cells) >= 17:
                section = cells[0].lower()
                name, nums = cells[1], cells[2:]
            elif section and len(cells) >= 17 and cells[0] == "Total":
                continue  # grand total futures+options, not needed
            elif section and len(cells) == 17:
                name, nums = cells[0], cells[1:]
            else:
                continue
            if name.startswith("Percentage"):
                continue
            out[section][name] = nums
        break
    return out


def _taifex_tv_ok(page: str, y: int, m: int) -> bool:
    """The page must echo the requested year/month in its selectors and contain the table."""
    if not re.search(rf'value="{m}"\s*selected', page) or not re.search(rf'value="{y}"\s*selected', page):
        return False
    tv = _taifex_participant_table(page)
    return bool(tv["futures"]) and bool(tv["options"])


def _taifex_shares(nums: list[str]) -> dict[str, float]:
    tot = to_num(nums[0])
    bs = [to_num(x) for x in nums[1:15]]
    pairs = [bs[i] + bs[i + 1] for i in range(0, 14, 2)]
    ind, secd, sit, forn, mf, other, prop = pairs
    two_sided = 2.0 * tot
    if abs(sum(pairs) - two_sided) > 0.001 * two_sided:
        log.warning("TAIFEX participant columns sum %s != 2*total %s", sum(pairs), two_sided)
    return shares_from_parts({
        "share_retail": ind,
        "share_foreign": forn,
        "share_local_inst": sit + mf + other,   # investment trusts, managed futures/futures trusts, other institutions
        "share_prop": secd + prop,              # securities dealers + FCM proprietary (incl. market makers)
    })


def _taifex_daily_volume(month: str, product: str, is_option: bool) -> tuple[float, int]:
    """Sum of daily volume (non-spread rows, both sessions) and number of trading days."""
    start, end = month_bounds(month)
    y, m = _ym(month)
    url = TAIFEX_OPT_DL if is_option else TAIFEX_FUT_DL
    body = _fetch("taifex", f"taifex_{'opt' if is_option else 'fut'}_{product}_{y:04d}{m:02d}.csv.gz",
                  url=url, method="POST",
                  data={"down_type": "1", "commodity_id": product, "commodity_id2": "",
                        "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d")},
                  validate=lambda b: b[:40].lower().startswith(b"date,") and b.count(b"\n") > 5,
                  timeout=240)
    text = body.decode("cp950", errors="replace")
    rdr = csv.reader(io.StringIO(text))
    header = [h.strip().lower() for h in next(rdr)]
    i_date = header.index("date")
    i_cm = next(i for i, h in enumerate(header) if h.startswith("contract month"))
    i_vol = header.index("volume")
    total = 0.0
    days = set()
    for r in rdr:
        if len(r) <= i_vol or not r[i_date].strip():
            continue
        if "/" in r[i_cm]:  # calendar spread combination rows are not part of the official total
            continue
        v = r[i_vol].strip()
        if v in ("", "-"):
            continue
        total += to_num(v)
        days.add(r[i_date].strip())
    return total, len(days)


def _taifex_contract_values(month: str, kind: str) -> dict[str, tuple[float, float | None]]:
    """{product: (notional_TWD, premium_TWD or None)} from contractMonthDataDown. Values are
    published in millions of TWD -> multiplied by 1e6."""
    y, m = _ym(month)
    body = _fetch("taifex", f"taifex_ctv_{kind}_{y:04d}{m:02d}.csv", url=TAIFEX_CTV, method="POST",
                  data={"queryMonth": f"{y:04d}{m:02d}", "queryTypeMonth": kind},
                  validate=lambda b: b.lower().startswith(b"data month") and f"{y:04d}{m:02d}".encode() in b)
    text = body.decode("cp950", errors="replace")
    head = text[:400].lower()
    if "millions" not in head:
        raise ValueError(f"TAIFEX contract value units changed: {text[:200]!r}")
    out = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not re.fullmatch(r"\d{6}", parts[0]):
            continue  # header (the OPT header spans two physical lines)
        notional = to_num(parts[2]) * 1e6
        premium = to_num(parts[3]) * 1e6 if kind == "OPT" and len(parts) > 3 and parts[3] else None
        out[parts[1]] = (notional, premium)
    return out


def _taifex_month(month: str) -> list[dict]:
    y, m = _ym(month)
    out: list[dict] = []
    tag = f"{y:04d}{m:02d}"

    def R(segment, product, metric, value, source, **kw):
        out.append(row(month=month, country=COUNTRY, exchange="TAIFEX", segment=segment, product=product,
                       metric=metric, value=value, source=source, **kw))

    # --- category totals + participant shares (contracts) -------------------------
    try:
        body = _fetch("taifex", f"taifex_tradingVolume_{tag}.html", url=TAIFEX_TV, method="POST",
                      data={"syear": str(y), "smonth": str(m)},
                      validate=lambda b: _taifex_tv_ok(b.decode("utf-8", "replace"), y, m))
        page = body.decode("utf-8", errors="replace")
        tv = _taifex_participant_table(page)
        src = f"{TAIFEX_TV} (POST syear={y}&smonth={m})"
        fut_total = tv["futures"].get("Trading Volume of Futures")
        # TAIFEX labels the options subtotal row "Trading Volume of Futures" as well (site typo).
        opt_total = tv["options"].get("Trading Volume of Options") or tv["options"].get("Trading Volume of Futures")
        if fut_total:
            R("futures", "ALL", "contracts", to_num(fut_total[0]), src)
            for k, v in _taifex_shares(fut_total).items():
                R("futures", "ALL", k, v, src, basis="contracts")
        if opt_total:
            R("options", "ALL", "contracts", to_num(opt_total[0]), src)
            for k, v in _taifex_shares(opt_total).items():
                R("options", "ALL", k, v, src, basis="contracts")
        # STF participant shares: the "Single Stock Futures" category row also contains ETF
        # futures (~4% of the category in 2026), so these shares are slightly contaminated.
        stf = tv["futures"].get("Single Stock Futures")
        if stf:
            for k, v in _taifex_shares(stf).items():
                R("futures", "STF", k, v, src, basis="contracts")
    except Exception as e:  # noqa: BLE001
        log.warning("TAIFEX tradingVolume %s skipped: %s", month, e)

    # --- STF contracts (pure single stock futures, ex ETF futures) -----------------
    try:
        body = _fetch("taifex", f"taifex_monthlyFutVolData_{tag}.html", url=TAIFEX_MFV, method="POST",
                      data={"queryMonth": tag}, validate=lambda b: b"Single Stock Futures" in b and tag.encode() in b)
        page = body.decode("utf-8", errors="replace")
        found = False
        for table in _html_tables(page):
            for cells in table:
                if len(cells) >= 3 and cells[0] == tag and cells[1] == "Single Stock Futures":
                    R("futures", "STF", "contracts", to_num(cells[2]), f"{TAIFEX_MFV} (POST queryMonth={tag})")
                    found = True
                    break
            if found:
                break
        if not found:
            raise ValueError("Single Stock Futures row not found")
    except Exception as e:  # noqa: BLE001
        log.warning("TAIFEX monthlyFutVolData %s skipped: %s", month, e)

    # --- notional / premium per product (millions TWD -> TWD) ---------------------
    for kind, segment, products in (("FUT", "futures", TAIFEX_FUT_PRODUCTS + ["STF"]),
                                    ("OPT", "options", TAIFEX_OPT_PRODUCTS)):
        try:
            vals = _taifex_contract_values(month, kind)
            src = f"{TAIFEX_CTV} (POST queryMonth={tag}&queryTypeMonth={kind}; page {TAIFEX_CTV_PAGE})"
            R(segment, "ALL", "turnover_notional", sum(v[0] for v in vals.values()), src, currency=CCY)
            if kind == "OPT":
                R(segment, "ALL", "turnover_premium", sum(v[1] or 0.0 for v in vals.values()), src, currency=CCY)
            for p in products:
                if p not in vals:
                    log.warning("TAIFEX %s %s: product %s missing from contract value file", month, kind, p)
                    continue
                R(segment, p, "turnover_notional", vals[p][0], src, currency=CCY)
                if kind == "OPT" and vals[p][1] is not None:
                    R(segment, p, "turnover_premium", vals[p][1], src, currency=CCY)
        except Exception as e:  # noqa: BLE001
            log.warning("TAIFEX contract values %s %s skipped: %s", month, kind, e)

    # --- contracts per product from daily downloads; trading days from TX -----------
    for p in TAIFEX_FUT_PRODUCTS:
        try:
            vol, ndays = _taifex_daily_volume(month, p, is_option=False)
            src = f"{TAIFEX_FUT_DL} (POST down_type=1&commodity_id={p}&queryStartDate..queryEndDate={month}; page {TAIFEX_DL_PAGE})"
            R("futures", p, "contracts", vol, src)
            if p == "TX":
                R("futures", "ALL", "trading_days", ndays, src)
                R("options", "ALL", "trading_days", ndays, src)
        except Exception as e:  # noqa: BLE001
            log.warning("TAIFEX daily volume %s %s skipped: %s", month, p, e)
    for p in TAIFEX_OPT_PRODUCTS:
        try:
            vol, _ = _taifex_daily_volume(month, p, is_option=True)
            src = f"{TAIFEX_OPT_DL} (POST down_type=1&commodity_id={p}&queryStartDate..queryEndDate={month}; page {TAIFEX_DL_PAGE})"
            R("options", p, "contracts", vol, src)
        except Exception as e:  # noqa: BLE001
            log.warning("TAIFEX daily volume %s %s skipped: %s", month, p, e)
    return out


# ----------------------------------------------------------------------------
def collect(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    for month in months:
        for name, fn in (("TWSE", _twse_month), ("TPEX", _tpex_month), ("TAIFEX", _taifex_month)):
            try:
                got = fn(month)
                rows.extend(got)
                log.info("%s %s: %d rows", name, month, len(got))
            except Exception as e:  # noqa: BLE001
                log.warning("%s %s failed: %s", name, month, e)
    return rows


if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ms = sys.argv[1:] or common.last_n_months(12)
    for r in collect(ms):
        print(r)
