"""Korea (KRX) collector: cash equities + exchange-traded derivatives.

Produces rows for country KR / exchange KRX for the given list of "YYYY-MM"
months. Two upstream sources are used, because the primary KRX data portal
is now partly login-gated:

1. KRX Data Marketplace (data.krx.co.kr) -- stocks segment
   ---------------------------------------------------------
   The regular statistics screens (menuId=MDC02...) and their JSON blds
   (``dbms/MDC/STAT/standard/MDCSTATxxxxx``) return HTTP 400 "LOGOUT" unless a
   member session exists (the portal switched to mandatory membership; pykrx
   now requires KRX_ID/KRX_PW for the same reason). However the portal also
   serves an "outer loader" embed of the investor-type screen that is NOT
   login-gated and returns identical data:

     page : https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022
     api  : POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
            bld = dbms/MDC_OUT/STAT/standard/MDCSTAT02201_OUT  (period totals by investor)
            bld = dbms/MDC_OUT/STAT/standard/MDCSTAT02202_OUT  (daily series)
            mktId = STK (KOSPI) | KSQ (KOSDAQ) | KNX (KONEX) | ALL
            strtDd/endDd = YYYYMMDD, inqTpCd = 1 (period) | 2 (daily)
            trdVolVal = 1 (volume) | 2 (value), askBid = 1 sell | 2 buy | 3 net
            NOTE: do not send segTpCd (it restricts to the KOSDAQ-Global segment).
   Values are returned in full KRW (not millions): e.g. KOSPI 2025-09 total
   sell-side value = 254,193,390,632,655 KRW. Equivalent login-gated screen:
   menuId=MDC0201020301 "투자자별 거래실적" (bld MDCSTAT02201/02202).

   Investor-type mapping (value basis, buy+sell activity):
     개인                               -> share_retail
     외국인 + 기타외국인                 -> share_foreign
     금융투자 (financial investment =    -> share_prop   (securities firms' prop /
              securities companies)                     market-making books)
     보험, 투신, 사모, 은행, 기타금융,    -> share_local_inst
     연기금 등 (= 기관합계 - 금융투자)
     + 기타법인 (other corporations)     -> share_local_inst
   Check: 전체 = 기관합계 + 기타법인 + 개인 + 외국인 + 기타외국인.

2. KOSIS (kosis.kr, national statistics portal) -- derivatives segment
   ---------------------------------------------------------------------
   KRX files its official monthly "증권·파생상품시장통계" with KOSIS (orgId=343,
   statId=1976006, monthly, updated ~3 weeks after month end). The KRX portal's
   own derivatives screens (menuId MDC0201050xxx, e.g. "파생상품 투자자별
   거래실적" MDC0201050302) are login-gated and have no outer-loader variant,
   so KOSIS is used instead. KOSIS has no key-less REST API; we replay the
   statHtml web flow (statHtml.do -> right_layout.do -> statHtmlContent.do ->
   html.do) which returns the grid as an HTML table. Tables used:

     DT_343_2010_S0092  주가지수상품 총괄        (index products summary)
     DT_343_2010_S0093  주식상품총괄             (single-stock products summary)
     DT_343_2010_S0094  채권금리상품 총괄        (bond/rate products summary)
     DT_343_2010_S0182  통화기타상품 총괄        (currency & other products summary)
     DT_343_2010_S0095  코스피200선물 거래실적    (trading days, contracts, value)
     DT_343_2010_S0096  코스피200선물 투자자별 거래실적
     DT_343_2010_S0148  미니코스피200선물 투자자별 거래실적
     DT_343_2010_S0126  코스닥150선물 투자자별 거래실적
     DT_343_2010_S0101  3년국채선물 투자자별 거래실적
     DT_343_2010_S0158  10년국채선물 투자자별 거래실적
     DT_343_2010_S0104  미국달러선물 투자자별 거래실적
     DT_343_2010_S0112  주식선물 투자자별 거래실적
     DT_343_2010_S0106  코스피200옵션 투자자별 거래실적
     DT_343_2010_S0180  주식옵션 투자자별 거래실적
   Page URL pattern (used as `source`):
     https://kosis.kr/statHtml/statHtml.do?orgId=343&tblId=<tblId>&conn_path=I2
   Units: KOSIS reports money in 백만원 (million KRW) -> multiplied by 1e6.
   Futures "계약금액" is notional -> turnover_notional; options "거래대금" is
   premium -> turnover_premium. KRX counts a spread trade as 2 contracts
   (table footnote), consistent with its headline statistics.

   Derivatives investor-type mapping (contracts basis, buy+sell activity):
     개인 -> share_retail; 외국인 -> share_foreign; 금융투자 -> share_prop;
     (기관합계 - 금융투자) + 기타법인 -> share_local_inst.
   KOSIS publishes investor splits per product only. ALL-futures shares are
   derived by summing the per-product investor tables when they cover >=97%
   of ALL-futures contracts (they cover ~99.6%); ALL-options shares are
   skipped because weekly KOSPI200 options have no investor table.

Not available without a KRX member login / API key:
   * KOSPI200 weekly options as a separate product (KOSIS folds them into
     주가지수옵션합계; the KRX portal screen MDC0201050201 can split them).
   * All-futures / all-options investor shares straight from KRX
     (screen MDC0201050302, bld dbms/MDC/STAT/standard/MDCSTAT13101 family).
   * KRX Open API (openapi.krx.co.kr) needs an API key and has no investor split.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import re
import time
from pathlib import Path

import pandas as pd

import common
from common import log as _baselog, month_bounds, row, shares_from_parts, to_num

log = logging.getLogger("collectors.kr")

VENUES = ["KRX"]
COUNTRY = "KR"
EXCHANGE = "KRX"
CCY = "KRW"

RAW_DIR = Path(__file__).resolve().parent / "raw" / "kr"
SLEEP = 0.7  # seconds between HTTP requests

# --------------------------------------------------------------------------
# KRX Data Marketplace (outer-loader, not login-gated)
# --------------------------------------------------------------------------
KRX_OUTER_PAGE = "https://data.krx.co.kr/contents/MDC/MDI/outerLoader/index.cmd?screenId=MDCSTAT022"
KRX_JSON_API = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_BLD_PERIOD = "dbms/MDC_OUT/STAT/standard/MDCSTAT02201_OUT"  # 기간합계 (login-gated twin: MDCSTAT02201)
KRX_BLD_DAILY = "dbms/MDC_OUT/STAT/standard/MDCSTAT02202_OUT"   # 일별추이 (login-gated twin: MDCSTAT02202)
KRX_HEADERS = {"Referer": KRX_OUTER_PAGE, "X-Requested-With": "XMLHttpRequest"}
KRX_MARKETS = {"KOSPI": "STK", "KOSDAQ": "KSQ"}


def _krx_json(bld: str, params: dict, cache_name: str) -> dict:
    """POST to the KRX JSON API with on-disk caching of the raw response."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    fp = RAW_DIR / cache_name
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    data = {"bld": bld, "locale": "ko_KR"}
    data.update(params)
    r = common.post(KRX_JSON_API, data=data, headers=KRX_HEADERS)
    time.sleep(SLEEP)
    txt = r.text.strip()
    if txt == "LOGOUT" or not txt.startswith("{"):
        raise RuntimeError(f"KRX API refused ({txt[:40]!r}) for bld={bld}; portal login required")
    js = r.json()
    fp.write_text(json.dumps(js, ensure_ascii=False), encoding="utf-8")
    return js


def _krx_investor_period(mkt_id: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    p = {
        "mktId": mkt_id,
        "strtDd": start.strftime("%Y%m%d"),
        "endDd": end.strftime("%Y%m%d"),
        "inqTpCd": "1",
        "trdVolVal": "2",   # value
        "askBid": "3",      # response carries ASK/BID/NETBID columns anyway
        "share": "1",
        "money": "1",
    }
    js = _krx_json(KRX_BLD_PERIOD, p, f"krx_MDCSTAT02201_OUT_{mkt_id}_{month}.json")
    return js.get("output") or []


def _krx_daily(mkt_id: str, month: str) -> list[dict]:
    start, end = month_bounds(month)
    p = {
        "mktId": mkt_id,
        "strtDd": start.strftime("%Y%m%d"),
        "endDd": end.strftime("%Y%m%d"),
        "inqTpCd": "2",
        "trdVolVal": "2",
        "askBid": "1",
        "share": "1",
        "money": "1",
    }
    js = _krx_json(KRX_BLD_DAILY, p, f"krx_MDCSTAT02202_OUT_{mkt_id}_{month}.json")
    return js.get("output") or []


def _collect_stocks(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    src = KRX_OUTER_PAGE
    for month in months:
        per_mkt: dict[str, dict[str, float]] = {}  # market -> investor -> buy+sell value
        totals: dict[str, float] = {}
        for name, mkt in KRX_MARKETS.items():
            try:
                out = _krx_investor_period(mkt, month)
                inv = {}
                for rec in out:
                    k = rec.get("INVST_TP_NM", "").strip()
                    try:
                        ask = to_num(rec.get("ASK_TRDVAL"))
                        bid = to_num(rec.get("BID_TRDVAL"))
                    except ValueError:
                        continue
                    inv[k] = (ask, bid)
                if "전체" not in inv:
                    raise RuntimeError("no 전체 row")
                total_value = inv["전체"][0]  # sum of all sells == market traded value (KRW)
                if total_value <= 0:
                    raise RuntimeError("zero total value")
                totals[name] = total_value
                per_mkt[name] = {k: v[0] + v[1] for k, v in inv.items()}
                rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="stocks",
                                product=name, metric="turnover_notional", value=total_value,
                                currency=CCY, source=src))
                rows.extend(_stock_share_rows(month, name, per_mkt[name], src))
            except Exception as e:  # noqa: BLE001
                log.warning("KR stocks %s %s: %s", name, month, e)
        if len(totals) == len(KRX_MARKETS):
            rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="stocks",
                            product="ALL", metric="turnover_notional", value=sum(totals.values()),
                            currency=CCY, source=src))
            # ALL shares: sum KOSPI+KOSDAQ investor activity (KONEX excluded, ~0.01%)
            agg: dict[str, float] = {}
            for d in per_mkt.values():
                for k, v in d.items():
                    agg[k] = agg.get(k, 0.0) + v
            rows.extend(_stock_share_rows(month, "ALL", agg, src))
        # trading days: count daily rows for KOSPI
        try:
            daily = _krx_daily("STK", month)
            days = len({r.get("TRD_DD") for r in daily if r.get("TRD_DD")})
            if days <= 0:
                raise RuntimeError("no daily rows")
            rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="stocks",
                            product="ALL", metric="trading_days", value=days, source=src))
        except Exception as e:  # noqa: BLE001
            log.warning("KR stocks trading_days %s: %s", month, e)
    return rows


def _stock_share_rows(month: str, product: str, inv: dict[str, float], src: str) -> list[dict]:
    """inv: investor name -> buy+sell value. See module docstring for the mapping."""
    try:
        prop = inv["금융투자"]
        inst_total = inv["기관합계"]
        other_corp = inv.get("기타법인", 0.0)
        retail = inv["개인"]
        foreign = inv["외국인"] + inv.get("기타외국인", 0.0)
        parts = {
            "share_prop": prop,
            "share_local_inst": (inst_total - prop) + other_corp,
            "share_retail": retail,
            "share_foreign": foreign,
        }
        shares = shares_from_parts(parts)
    except (KeyError, ValueError) as e:
        log.warning("KR stocks shares %s %s: %s", product, month, e)
        return []
    return [row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="stocks", product=product,
                metric=m, value=v, basis="value", source=src) for m, v in shares.items()]


# --------------------------------------------------------------------------
# KOSIS statHtml replay client
# --------------------------------------------------------------------------
KOSIS_ORG = "343"
KOSIS_BASE = "https://kosis.kr"


def kosis_page_url(tbl: str) -> str:
    return f"{KOSIS_BASE}/statHtml/statHtml.do?orgId={KOSIS_ORG}&tblId={tbl}&conn_path=I2"


_LABEL_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _norm_label(s) -> str:
    s = str(s).strip()
    return _LABEL_PAREN.sub("", s).strip()


class KosisTable:
    """Parsed KOSIS grid: list of (label_set, {month: value}) rows."""

    def __init__(self, tbl: str, name: str, records: list[tuple[frozenset, dict]]):
        self.tbl = tbl
        self.name = name
        self.records = records

    def find(self, required: set[str], exclude: set[str] = frozenset()) -> list[dict]:
        req = {_norm_label(x) for x in required}
        exc = {_norm_label(x) for x in exclude}
        return [vals for labels, vals in self.records if req <= labels and not (exc & labels)]

    def one(self, required: set[str], exclude: set[str] = frozenset()) -> dict:
        hits = self.find(required, exclude)
        if len(hits) != 1:
            raise KeyError(f"{self.tbl}: {len(hits)} rows match {sorted(required)} minus {sorted(exclude)}")
        return hits[0]

    def sum(self, required_list: list[set[str]], exclude: set[str], month: str) -> float:
        total = 0.0
        for req in required_list:
            v = self.one(req, exclude).get(month)
            if v is None or (isinstance(v, float) and math.isnan(v)):
                raise ValueError(f"{self.tbl}: missing {sorted(req)} for {month}")
            total += v
        return total


def _kosis_fetch_html(tbl: str, months_yyyymm: list[str]) -> tuple[str, str]:
    """Replay the KOSIS statHtml flow. Returns (table_name, grid_html)."""
    S = common.SESSION
    page = kosis_page_url(tbl)
    r0 = common.get(page)
    time.sleep(SLEEP)
    base_params = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"',
                                  r0.text[r0.text.find('id="ParamInfo"'):r0.text.find("</form>")]))
    d = {"orgId": KOSIS_ORG, "tblId": tbl, "vwCd": "MT_ZTITLE", "dbUser": "NSI.", "language": "ko",
         "mode": "", "pub": "", "conn_path": "I2", "list_id": "", "itm_id": "", "orderBy": "",
         "serverType": "service", "dataOpt": "ko", "serverTypeOrigin": "service",
         "serverLocation": "NSO", "tblSe": "", "tblNm": "", "query": "", "tabYn": ""}
    r1 = common.post(f"{KOSIS_BASE}/statHtml/right_layout.do", data=d, headers={"Referer": page})
    time.sleep(SLEEP)
    m = re.search(r"paramInfoForm\.list_id\s*=\s*'([^']*)'", r1.text)
    list_id = m.group(1) if m else ""
    base_params.update({"orgId": KOSIS_ORG, "tblId": tbl, "pub": "2", "conn_path": "I2",
                        "obj_var_id": "A", "list_id": list_id, "vw_cd": "MT_ZTITLE", "itm_id": ""})
    r2 = common.post(f"{KOSIS_BASE}/statHtml/statHtmlContent.do", data=base_params,
                     headers={"Referer": f"{KOSIS_BASE}/statHtml/right_layout.do"}, timeout=90)
    time.sleep(SLEEP)
    t = r2.text
    a = t.find('id="ParamInfo"')
    b = t.find("</form>", a)
    P: dict[str, str] = {}
    for inp in re.findall(r"<input([^>]*)>", t[a:b]):
        n = re.search(r'name="([^"]+)"', inp)
        v = re.search(r'value="([^"]*)"', inp)
        if n:
            P[n.group(1)] = v.group(1) if v else ""
    i = t.find("var g_jsonStatInfo")
    if i < 0:
        raise RuntimeError(f"KOSIS {tbl}: metadata not found (page {len(t)} bytes)")
    info = json.JSONDecoder().raw_decode(t[t.find("'", i) + 1:])[0]
    name = info.get("tblNm", tbl)
    field_list = [{"targetId": "PRD", "targetValue": "", "prdValue": "M," + ",".join(months_yyyymm) + ",@"}]
    items = info["itemInfo"]["itmList"]
    for it in items:
        field_list.append({"targetId": "ITM_ID", "targetValue": it["itmId"], "prdValue": ""})
    order = ["TIME", "CHAR_ITM_ID"]
    ncell = len(months_yyyymm) * len(items)
    row_axis = []
    for ci, cls in enumerate(info["classInfoList"], 1):
        ids = [x.split("#")[-1] for x in cls["defaultItmList"]]
        for iid in ids:
            field_list.append({"targetId": f"OV_L{ci}_ID", "targetValue": iid, "prdValue": ""})
        order.append(f"OV_L{ci}_ID")
        ncell *= max(len(ids), 1)
        row_axis.append(cls["classId"])
    P.update({"fieldList": json.dumps(field_list, ensure_ascii=False), "orderStr": ",".join(order),
              "diviSearchYn": "N", "startNum": "1", "endNum": str(ncell), "viewKind": "1",
              "view": "table", "reqCellCnt": str(ncell), "isFirst": "Y", "classAllArr": "[]",
              "rowAxis": ",".join(row_axis), "colAxis": "TIME", "selectAllFlag": "N",
              "doAnal": "N", "tableType": "default"})
    r3 = S.post(f"{KOSIS_BASE}/statHtml/html.do", data=P, timeout=120,
                headers={"Referer": f"{KOSIS_BASE}/statHtml/statHtmlContent.do",
                         "X-Requested-With": "XMLHttpRequest"})
    time.sleep(SLEEP)
    r3.raise_for_status()
    js = r3.json()
    res = js.get("result")
    if not res:
        raise RuntimeError(f"KOSIS {tbl}: {js.get('errMsg', 'no result')!r}")
    return name, res[0]


def _kosis_table(tbl: str, months: list[str]) -> KosisTable:
    """Fetch (or load cached) KOSIS table for the given 'YYYY-MM' months."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    yyyymm = [m.replace("-", "") for m in months]
    fp = RAW_DIR / f"kosis_{tbl}_{yyyymm[0]}_{yyyymm[-1]}.html"
    meta = RAW_DIR / f"kosis_{tbl}_{yyyymm[0]}_{yyyymm[-1]}.name"
    if fp.exists():
        html = fp.read_text(encoding="utf-8")
        name = meta.read_text(encoding="utf-8") if meta.exists() else tbl
    else:
        last: Exception | None = None
        for attempt in range(3):
            try:
                name, html = _kosis_fetch_html(tbl, yyyymm)
                break
            except Exception as e:  # noqa: BLE001  (transient proxy / session hiccups)
                last = e
                log.info("KOSIS %s attempt %d failed: %s", tbl, attempt + 1, e)
                time.sleep(3.0 * (attempt + 1))
        else:
            raise RuntimeError(f"KOSIS {tbl} failed after 3 attempts: {last}")
        fp.write_text(html, encoding="utf-8")
        meta.write_text(name, encoding="utf-8")
    df = pd.read_html(io.StringIO(html))[0]
    label_cols = [c for c in df.columns if not re.match(r"^\d{4}\.\d{2}$", str(c[0]))]
    data_cols = [c for c in df.columns if re.match(r"^\d{4}\.\d{2}$", str(c[0])) and c[1] == "원데이터"]
    df = df.dropna(how="all")
    df[label_cols] = df[label_cols].ffill()
    records: list[tuple[frozenset, dict]] = []
    seen = set()
    for _, r in df.iterrows():
        labels = frozenset(_norm_label(r[c]) for c in label_cols if str(r[c]) != "nan")
        key = (labels, tuple(str(r[c]) for c in data_cols))
        if key in seen:
            continue
        seen.add(key)
        vals = {}
        for c in data_cols:
            month = str(c[0]).replace(".", "-")
            try:
                vals[month] = to_num(r[c])
            except (ValueError, TypeError):
                vals[month] = float("nan")
        records.append((labels, vals))
    return KosisTable(tbl, name, records)


# --------------------------------------------------------------------------
# Derivatives from KOSIS
# --------------------------------------------------------------------------
MILLION = 1_000_000.0  # KOSIS money unit is 백만원

# investor labels used by the KOSIS derivatives tables
INV_RETAIL = "개인"
INV_FOREIGN = "외국인"
INV_PROP = "금융투자"
INV_INST_TOTAL = "기관합계"
INV_OTHER_CORP = "기타법인"
INV_TOTAL = "전체"

EXCL_SUBMARKET = {"정규", "야간"}  # keep only 합계(TOT) rows where RTH/NTH split exists
EXCL_STATS = {"일평균", "최대", "최소", "구성비", "순매수", "스프레드"}
EXCL = EXCL_SUBMARKET | EXCL_STATS

# product definitions: investor table id, contracts label, value label, metric for value,
# option legs (call/put labels) if the table is split by option type
FUT_PRODUCTS = {
    # product : (tblId, contracts_label, value_label)
    "KOSPI200F": ("DT_343_2010_S0096", "계약수", "계약금액"),
    "KOSPI200MF": ("DT_343_2010_S0148", "계약수", "계약금액"),
    "KOSDAQ150F": ("DT_343_2010_S0126", "계약수", "계약금액"),
    "KTB3F": ("DT_343_2010_S0101", "계약수", "계약금액"),
    "KTB10F": ("DT_343_2010_S0158", "계약수", "계약금액"),
    "USDF": ("DT_343_2010_S0104", "계약수", "계약금액"),
    "SSF": ("DT_343_2010_S0112", "계약수", "계약금액"),
}
OPT_PRODUCTS = {
    # product : (tblId, contracts_label, value_label(premium), legs)
    "KOSPI200O": ("DT_343_2010_S0106", "거래량", "거래대금", ("콜", "풋")),
    "SSO": ("DT_343_2010_S0180", "계약수", "계약금액", ("콜", "풋")),
}
TBL_K200F_PERF = "DT_343_2010_S0095"   # trading days
TBL_SUM_INDEX = "DT_343_2010_S0092"
TBL_SUM_STOCK = "DT_343_2010_S0093"
TBL_SUM_BOND = "DT_343_2010_S0094"
TBL_SUM_CCY = "DT_343_2010_S0182"


def _inv_activity(t: KosisTable, measure: str, legs: tuple[str, ...] | tuple[()], month: str,
                  investor: str) -> float:
    """buy+sell activity for an investor (summed over option legs if any)."""
    total = 0.0
    for side in ("매도", "매수"):
        for leg in (legs or ("",)):
            req = {measure, investor, side}
            if leg:
                req.add(leg)
            v = t.one(req, EXCL).get(month)
            if v is None or math.isnan(v):
                raise ValueError(f"{t.tbl}: missing {investor} {side} {leg} {month}")
            total += v
    return total


def _inv_parts(t: KosisTable, measure: str, legs, month: str) -> dict[str, float]:
    prop = _inv_activity(t, measure, legs, month, INV_PROP)
    inst = _inv_activity(t, measure, legs, month, INV_INST_TOTAL)
    retail = _inv_activity(t, measure, legs, month, INV_RETAIL)
    foreign = _inv_activity(t, measure, legs, month, INV_FOREIGN)
    try:
        other = _inv_activity(t, measure, legs, month, INV_OTHER_CORP)
    except (KeyError, ValueError):
        other = 0.0
    return {"share_prop": prop, "share_local_inst": (inst - prop) + other,
            "share_retail": retail, "share_foreign": foreign}


def _total(t: KosisTable, measure: str, legs, month: str) -> float:
    """Market total = 전체 sell side (summed over legs)."""
    reqs = [{measure, INV_TOTAL, "매도"} | ({leg} if leg else set()) for leg in (legs or ("",))]
    return t.sum(reqs, EXCL, month)


def _collect_derivatives(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    # --- per-product tables -------------------------------------------------
    fut_parts_by_month: dict[str, dict[str, float]] = {m: {} for m in months}
    fut_covered_contracts: dict[str, float] = {m: 0.0 for m in months}
    for seg, products in (("futures", FUT_PRODUCTS), ("options", OPT_PRODUCTS)):
        for product, spec in products.items():
            tbl, c_label, v_label = spec[0], spec[1], spec[2]
            legs = spec[3] if len(spec) > 3 else ()
            src = kosis_page_url(tbl)
            try:
                t = _kosis_table(tbl, months)
            except Exception as e:  # noqa: BLE001
                log.warning("KR %s %s: table %s failed: %s", seg, product, tbl, e)
                continue
            for month in months:
                try:
                    contracts = _total(t, c_label, legs, month)
                    rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment=seg,
                                    product=product, metric="contracts", value=contracts, source=src))
                    value = _total(t, v_label, legs, month) * MILLION
                    vmetric = "turnover_premium" if seg == "options" else "turnover_notional"
                    rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment=seg,
                                    product=product, metric=vmetric, value=value, currency=CCY, source=src))
                except Exception as e:  # noqa: BLE001
                    log.warning("KR %s %s %s totals: %s", seg, product, month, e)
                    continue
                try:
                    parts = _inv_parts(t, c_label, legs, month)
                    for m_, v_ in shares_from_parts(parts).items():
                        rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment=seg,
                                        product=product, metric=m_, value=v_, basis="contracts", source=src))
                    if seg == "futures":
                        agg = fut_parts_by_month[month]
                        for k, v in parts.items():
                            agg[k] = agg.get(k, 0.0) + v
                        fut_covered_contracts[month] += contracts
                except Exception as e:  # noqa: BLE001
                    log.warning("KR %s %s %s shares: %s", seg, product, month, e)

    # --- trading days (KOSPI200 futures performance table) ------------------
    try:
        t = _kosis_table(TBL_K200F_PERF, months)
        for month in months:
            try:
                days = t.one({"계", "거래일수"}, EXCL).get(month)
                if days is None or math.isnan(days):
                    raise ValueError("nan")
                for seg in ("futures", "options"):
                    rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment=seg,
                                    product="ALL", metric="trading_days", value=days,
                                    source=kosis_page_url(TBL_K200F_PERF)))
            except Exception as e:  # noqa: BLE001
                log.warning("KR derivatives trading_days %s: %s", month, e)
    except Exception as e:  # noqa: BLE001
        log.warning("KR derivatives trading_days table: %s", e)

    # --- ALL futures / ALL options from the summary tables -------------------
    sums = {}
    for key, tbl in (("index", TBL_SUM_INDEX), ("stock", TBL_SUM_STOCK),
                     ("bond", TBL_SUM_BOND), ("ccy", TBL_SUM_CCY)):
        try:
            sums[key] = _kosis_table(tbl, months)
        except Exception as e:  # noqa: BLE001
            log.warning("KR derivatives summary %s (%s): %s", key, tbl, e)
    all_src = ";".join(kosis_page_url(t) for t in (TBL_SUM_INDEX, TBL_SUM_STOCK, TBL_SUM_BOND, TBL_SUM_CCY))

    def _pick(t: KosisTable, group: str, measure: str, month: str, optional=False) -> float:
        try:
            v = t.one({group, measure}).get(month)
        except KeyError:
            if optional:
                return 0.0
            raise
        if v is None or math.isnan(v):
            if optional:
                return 0.0
            raise ValueError(f"{t.tbl}: {group}/{measure} missing for {month}")
        return v

    for month in months:
        if len(sums) == 4:
            try:
                fc = (_pick(sums["index"], "주가지수선물합계", "거래량", month)
                      + _pick(sums["bond"], "채권금리선물 합계", "계약수", month)
                      + _pick(sums["ccy"], "통화기타선물합계", "계약수", month)
                      + _pick(sums["stock"], "주식선물", "거래량", month))
                fv = (_pick(sums["index"], "주가지수선물합계", "계약금액", month)
                      + _pick(sums["bond"], "채권금리선물 합계", "계약금액", month)
                      + _pick(sums["ccy"], "통화기타선물합계", "계약금액", month)
                      + _pick(sums["stock"], "주식선물", "계약금액", month)) * MILLION
                rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="futures",
                                product="ALL", metric="contracts", value=fc, source=all_src))
                rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="futures",
                                product="ALL", metric="turnover_notional", value=fv, currency=CCY,
                                source=all_src))
                # ALL-futures investor shares from per-product tables, if coverage is high enough
                cov = fut_covered_contracts.get(month, 0.0)
                if fc > 0 and cov / fc >= 0.97 and fut_parts_by_month[month]:
                    for m_, v_ in shares_from_parts(fut_parts_by_month[month]).items():
                        rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="futures",
                                        product="ALL", metric=m_, value=v_, basis="contracts",
                                        source=";".join(kosis_page_url(s[0]) for s in FUT_PRODUCTS.values())))
                else:
                    log.info("KR ALL futures shares skipped for %s (coverage %.1f%%)", month,
                             100 * cov / fc if fc else 0)
            except Exception as e:  # noqa: BLE001
                log.warning("KR ALL futures %s: %s", month, e)
            try:
                oc = (_pick(sums["index"], "주가지수옵션합계", "거래량", month)
                      + _pick(sums["stock"], "주식옵션소계", "거래량", month)
                      + _pick(sums["ccy"], "통화기타옵션합계", "계약수", month, optional=True))
                ov = (_pick(sums["index"], "주가지수옵션합계", "거래대금", month)
                      + _pick(sums["stock"], "주식옵션소계", "거래대금", month)
                      + _pick(sums["ccy"], "통화기타옵션합계", "계약금액", month, optional=True)) * MILLION
                rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="options",
                                product="ALL", metric="contracts", value=oc, source=all_src))
                rows.append(row(month=month, country=COUNTRY, exchange=EXCHANGE, segment="options",
                                product="ALL", metric="turnover_premium", value=ov, currency=CCY,
                                source=all_src))
            except Exception as e:  # noqa: BLE001
                log.warning("KR ALL options %s: %s", month, e)
    return rows


# --------------------------------------------------------------------------
def collect(months: list[str]) -> list[dict]:
    rows: list[dict] = []
    try:
        rows.extend(_collect_stocks(months))
    except Exception as e:  # noqa: BLE001
        log.error("KR stocks collection failed: %s", e)
    try:
        rows.extend(_collect_derivatives(months))
    except Exception as e:  # noqa: BLE001
        log.error("KR derivatives collection failed: %s", e)
    return rows


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from .common import last_n_months
    out = collect(last_n_months(12))
    df = pd.DataFrame(out)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(RAW_DIR / "preview.csv", index=False)
    print(df.groupby(["segment", "product", "metric"]).size())
