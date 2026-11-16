# ✈️ Capital Boarding Pass — International Cost of Capital

**▶️ Live app:** _add the Streamlit link here after deploying_

A Streamlit app that answers: *"What return must this project earn — in U.S. dollars and in the local currency of the country where it happens?"* It compares the **cost of equity, cost of debt and WACC** in USD and in the selected country's currency, side by side, with plain-English explanations. Built for an MBA International Finance assignment.

The interface is designed as a trip: a **boarding pass** says where you are (industry, USD → destination currency, tax, ERP, debt share), an airport-style **departure board** shows the hurdle rates, and a **world map** lets you "fly" the same project to any country.

## What the app does

1. **Step 1 – plan your flight:** pick an industry (Damodaran), a destination country, the equity risk premium, the target debt share, and whether the project uses the **U.S. or the foreign corporate tax rate** (one switch).
2. **Step 2 – boarding pass + departure board:** risk-free rate, levered beta, cost of equity, BBB spread, cost of debt (pre- and after-tax) and WACC in **USD vs local currency**, with the gap in percentage points and an "FX turbulence" label.
3. **Step 3 – plain-English verdict:** where you stand, opportunities and watch-outs (rule-based, 3 bullets max).
4. **Step 4 – explore:** 🌍 world map of local-currency WACC (click a dot to change country) · 🏭 all-industry comparison (chart + table) · 🧮 step-by-step math with your numbers · 📡 data sources and dates · ✅ live self-checks · 📥 CSV downloads (this result, all industries, all countries).
5. **Sidebar "control tower":** 🔄 **Refresh data** button, live/backup status of each source, and **manual overrides** for unlevered beta, U.S. and foreign tax rates, USD BBB spread, both 10-year yields and the foreign BBB spread (when off, the auto value is shown).

**Defaults:** Software (System & Application) · United States · U.S. tax rate · ERP 6.5% · 30% debt.

## Formulas

All rates are annual decimals. `w_D` = debt share D/(D+E), `w_E = 1 − w_D`, `t` = project tax rate (U.S. or foreign, same in both currencies).

| Step | Formula |
|---|---|
| Debt-to-equity | `D/E = w_D / (1 − w_D)` |
| Relevered beta (Hamada) | `β_L = β_U × [1 + (1 − t) × D/E]` |
| Cost of equity, USD | `k_E,USD = y_US + β_L × ERP` |
| Cost of debt, USD | `k_D,USD = y_US + s_BBB` (after tax: `× (1 − t)`) |
| WACC, USD | `w_E × k_E,USD + w_D × k_D,USD × (1 − t)` |
| Parity factor | `F = (1 + y_FC) / (1 + y_US)` |
| Cost of equity, FC | `k_E,FC = (1 + k_E,USD) × F − 1` |
| Foreign spread (auto) | `s_FC = (1 + k_D,USD) × F − 1 − y_FC = s_BBB × F` (or manual override) |
| Cost of debt, FC | `k_D,FC = y_FC + s_FC` |
| WACC, FC | `w_E × k_E,FC + w_D × k_D,FC × (1 − t)` |

`y` = 10-year government bond yield. The bond-yield gap is read as the market's expected inflation/depreciation gap (international Fisher effect); the multiplicative form keeps compounding exact.

### Properties the app verifies live (tab ✅ Self-checks, and `python checks.py`)
* **Equal yields ⇒ equal results:** if `y_FC = y_US` then `F = 1`, so every foreign number equals its USD twin (with the foreign-spread override off).
* **Zero debt ⇒ WACC = cost of equity:** `w_D = 0` gives `β_L = β_U` and `WACC = k_E` in both currencies.
* **Foreign-spread override leaves USD unchanged:** the USD block never reads `s_FC`.

## Data sources (latest available, cached 12 h, 🔄 refresh button)

| Input | Source | What is read |
|---|---|---|
| Unlevered beta | Damodaran, [betas.xls](https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls) | Sheet *Industry Averages*, column **Unlevered beta** (U.S. firms); date from "Date updated" |
| USD BBB spread | Damodaran, [ratings.html](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html) | Row **Baa2/BBB** of the table *for large non-financial service firms*; date from "Data used is as of" |
| Corporate tax | OECD [Corporate Tax Statistics](https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/full-report/statutory-corporate-income-tax-rates_440f9286.html) | The OECD web page blocks automated reading (HTTP 403), so the app reads the **same database** through the official OECD SDMX API: dataflow `OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT`, combined (central + sub-central) statutory rate `CIT_C`, latest year per country |
| 10-year yields | Trading Economics, [bonds](https://tradingeconomics.com/bonds) | No API key: the *Government Bond 10Y* country table is scraped from `tradingeconomics.com/country-list/government-bond-10y` (fallback `/bonds`); the quote month of each country is shown |

Countries offered = Trading Economics 10Y list ∩ OECD tax data (≈ 53 countries; e.g. Russia and Taiwan have no OECD rate and are left out).

**Backup:** if any live source fails (Streamlit Cloud can be blocked by some sites), the app uses `backup_snapshot.json` for that source and shows a yellow banner plus a BACKUP pill. Refresh the snapshot locally with `python build_backup.py`.

## Files

| File | Purpose |
|---|---|
| `app.py` | The app (fetch → fallback → compute → verdict → charts → UI). `compute()` is pure Python. |
| `checks.py` | Hand-checkable example + the three verification tests (`python checks.py`) |
| `build_backup.py` | Rebuilds `backup_snapshot.json` from the live sources |
| `backup_snapshot.json` | Offline copy of all four sources |
| `.streamlit/config.toml` | Theme colors |

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy on Streamlit Community Cloud

1. Push this repo to GitHub (branch `main`, files at the top level).
2. <https://share.streamlit.io> → **Create app** → this repo, branch `main`, main file `app.py`.
3. **Advanced settings → Python 3.12**; leave Secrets empty → **Deploy**.

## Caveats

* Betas are U.S. industry averages; the same beta is used in every country.
* No separate country-risk premium; high sovereign yields partly carry country risk into the local rate.
* The foreign BBB spread is the U.S. spread translated by parity unless overridden; real local borrowing can cost more.
* Euro-area countries share the EUR but have different sovereign yields, so their EUR hurdle rates differ.
* Some Trading Economics quotes can be old (e.g. Bulgaria); the app flags quotes not from the current month.
* Educational tool, not investment advice.
