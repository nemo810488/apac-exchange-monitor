# Manual entries

Add one or more CSV files named `manual_<anything>.csv` next to this file (e.g. `manual_hk.csv`). They are
merged into `observations.csv` on every run, and **a manual row always wins**
over an automated row with the same key, so this is also where you correct a
collector that got something wrong.

Columns (same as `observations.csv`; `method` and `collected_at` are filled in for you):

| column | example | notes |
|---|---|---|
| month | `2026-09` | |
| country | `HK` | must match `venues.csv` |
| exchange | `HKEX` | code from `venues.csv` |
| segment | `futures` | `stocks` / `futures` / `options` |
| product | `ALL` or `HSIF` | `ALL` = whole segment |
| metric | `turnover_notional` | see list below |
| basis | `contracts` | only for `share_*` rows: `value` / `contracts` / `premium` |
| value | `1234567890000` | base currency units (not millions); counts; or a 0–1 fraction |
| currency | `HKD` | only for `turnover_*` rows |
| source | `https://…` | the page or file the number came from |

Metrics: `turnover_notional`, `turnover_premium`, `contracts`, `trading_days`,
`share_retail`, `share_foreign`, `share_local_inst`, `share_prop`.

The dashboard needs at least `turnover_notional` (or `contracts`) **and**
`trading_days` for a venue/segment/month to show an ADV. Shares are optional and
should be two-sided (buy + sell) fractions that sum to ~1.

`manual_template.csv` holds just the header. Example rows (fictional numbers):

```
2026-09,HK,HKEX,futures,ALL,contracts,,14500000,,https://www.hkex.com.hk/...
2026-09,HK,HKEX,futures,ALL,trading_days,,21,,https://www.hkex.com.hk/...
2026-09,HK,HKEX,stocks,ALL,turnover_notional,,3100000000000,HKD,https://www.hkex.com.hk/...
2026-09,TH,SET,stocks,ALL,share_retail,value,0.33,,https://www.set.or.th/...
```
