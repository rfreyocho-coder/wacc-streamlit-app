"""Capital Boarding Pass: an international cost-of-capital app.

Compares a project's cost of equity, cost of debt and WACC in U.S. dollars and
in the currency of a selected country, using live data:

    * Industry unlevered betas ........ Damodaran (betas.xls, U.S. firms)
    * BBB (Baa2) default spread ........ Damodaran (ratings.html, large non-financial firms)
    * Corporate tax rates .............. OECD Corporate Tax Statistics (SDMX API)
    * 10-year government bond yields ... Trading Economics (scraped, no key)

Layout of this file (top to bottom):
    constants -> country table -> fetch (live) -> fallback (backup JSON)
    -> compute (pure math) -> verdict (plain-English text) -> charts -> UI

The math lives in `compute()` and has no Streamlit calls, so it can be
imported and tested on its own (see `checks.py`).
"""
from __future__ import annotations

import html
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# CONSTANTS
# ----------------------------------------------------------------------------
BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ratings.html"
OECD_PAGE_URL = ("https://www.oecd.org/en/publications/corporate-tax-statistics-2024_9c27d6e8-en/"
                 "full-report/statutory-corporate-income-tax-rates_440f9286.html")
# Same OECD database, machine-readable. Combined (central + sub-central) statutory CIT rate.
OECD_API_URL = ("https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,DSD_TAX_CIT@DF_CIT,2.0/"
                ".A.CIT_C.ST.PT_INC_TAX.S13._Z._Z._Z?startPeriod=2020"
                "&dimensionAtObservation=AllDimensions&format=csvfilewithlabels")
TE_PAGE_URL = "https://tradingeconomics.com/bonds"
TE_TABLE_URLS = ["https://tradingeconomics.com/country-list/government-bond-10y",
                 "https://tradingeconomics.com/bonds"]

HTTP_TIMEOUT = 30
CACHE_TTL = 12 * 3600
BACKUP_PATH = Path(__file__).resolve().parent / "backup_snapshot.json"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_INDUSTRY = "Software (System & Application)"
DEFAULT_COUNTRY = "USA"
DEFAULT_ERP = 6.5      # %
DEFAULT_DEBT = 30      # % of capital

# FX-turbulence bands for |WACC_local - WACC_USD| in percentage points.
TURBULENCE = [(1.0, "LOW", "#2BA84A"), (4.0, "MODERATE", "#F2A900"), (np.inf, "HIGH", "#E4572E")]

# Palette (also used by the CSS below)
NAVY, SKY, AMBER, CORAL, GREEN, CREAM = "#0B1F3A", "#3E7CB1", "#FFC72C", "#E4572E", "#2BA84A", "#FFF8EC"

# Trading Economics country name | ISO3 | ISO2 (flag) | currency
_COUNTRY_TABLE = """
Australia|AUS|AU|AUD
Austria|AUT|AT|EUR
Belgium|BEL|BE|EUR
Brazil|BRA|BR|BRL
Bulgaria|BGR|BG|BGN
Canada|CAN|CA|CAD
Chile|CHL|CL|CLP
China|CHN|CN|CNY
Colombia|COL|CO|COP
Croatia|HRV|HR|EUR
Czech Republic|CZE|CZ|CZK
Denmark|DNK|DK|DKK
Finland|FIN|FI|EUR
France|FRA|FR|EUR
Germany|DEU|DE|EUR
Greece|GRC|GR|EUR
Hong Kong|HKG|HK|HKD
Hungary|HUN|HU|HUF
Iceland|ISL|IS|ISK
India|IND|IN|INR
Indonesia|IDN|ID|IDR
Ireland|IRL|IE|EUR
Israel|ISR|IL|ILS
Italy|ITA|IT|EUR
Japan|JPN|JP|JPY
Kenya|KEN|KE|KES
Lithuania|LTU|LT|EUR
Malaysia|MYS|MY|MYR
Mexico|MEX|MX|MXN
Netherlands|NLD|NL|EUR
New Zealand|NZL|NZ|NZD
Nigeria|NGA|NG|NGN
Norway|NOR|NO|NOK
Pakistan|PAK|PK|PKR
Peru|PER|PE|PEN
Philippines|PHL|PH|PHP
Poland|POL|PL|PLN
Portugal|PRT|PT|EUR
Romania|ROU|RO|RON
Russia|RUS|RU|RUB
Singapore|SGP|SG|SGD
Slovakia|SVK|SK|EUR
Slovenia|SVN|SI|EUR
South Africa|ZAF|ZA|ZAR
South Korea|KOR|KR|KRW
Spain|ESP|ES|EUR
Sweden|SWE|SE|SEK
Switzerland|CHE|CH|CHF
Taiwan|TWN|TW|TWD
Thailand|THA|TH|THB
Turkey|TUR|TR|TRY
United Kingdom|GBR|GB|GBP
United States|USA|US|USD
Vietnam|VNM|VN|VND
Zambia|ZMB|ZM|ZMW
"""
COUNTRIES = {}
TE_NAME_TO_ISO = {}
for _line in _COUNTRY_TABLE.strip().splitlines():
    _te, _iso3, _iso2, _cur = _line.split("|")
    COUNTRIES[_iso3] = {"name": _te, "iso2": _iso2, "cur": _cur}
    TE_NAME_TO_ISO[_te.lower()] = _iso3


def flag(iso3: str) -> str:
    iso2 = COUNTRIES.get(iso3, {}).get("iso2", "")
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso2) if len(iso2) == 2 else "🏳️"


# ----------------------------------------------------------------------------
# FETCH (live) - each returns a plain dict; raise on any problem
# ----------------------------------------------------------------------------
def _get(url: str, **headers) -> requests.Response:
    last = None
    for _ in range(2):  # one retry
        try:
            r = requests.get(url, headers={**HEADERS, **headers}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"{url}: {last}")


def fetch_betas_live() -> dict:
    raw = pd.read_excel(io.BytesIO(_get(BETAS_URL).content),
                        sheet_name="Industry Averages", header=None)
    as_of = None
    for i in range(min(10, len(raw))):
        if str(raw.iat[i, 0]).strip().lower().startswith("date updated"):
            as_of = pd.to_datetime(raw.iat[i, 1]).date().isoformat()
    hdr = raw.index[raw[0].astype(str).str.strip().eq("Industry Name")][0]
    cols = [str(c).strip() for c in raw.iloc[hdr]]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = cols
    df = df.loc[:, ~pd.Index(cols).duplicated()]
    out = pd.DataFrame({
        "industry": df["Industry Name"].astype(str).str.strip(),
        "firms": pd.to_numeric(df["Number of firms"], errors="coerce"),
        "beta": pd.to_numeric(df["Beta"], errors="coerce"),
        "de_ratio": pd.to_numeric(df["D/E Ratio"], errors="coerce"),
        "beta_u": pd.to_numeric(df["Unlevered beta"], errors="coerce"),
    }).dropna(subset=["beta_u"])
    out = out[out["industry"].ne("nan")].reset_index(drop=True)
    if len(out) < 50:
        raise ValueError("betas.xls: fewer industries than expected")
    return {"table": out, "as_of": as_of or "unknown"}


def fetch_spread_live() -> dict:
    page = _get(RATINGS_URL).text
    m = re.search(r"as of\s*([A-Za-z]+\s+\d{4})", re.sub(r"<[^>]+>", " ", page))
    for table in pd.read_html(io.StringIO(page)):
        for _, row in table.iterrows():
            vals = [str(v).strip() for v in row.values]
            for j, v in enumerate(vals[:-1]):
                if v.replace(" ", "") == "Baa2/BBB":   # first hit = large non-financial firms
                    spread = float(vals[j + 1].replace("%", "")) / 100
                    if not 0 < spread < 0.2:
                        raise ValueError("BBB spread out of range")
                    return {"value": spread, "as_of": m.group(1) if m else "unknown"}
    raise ValueError("Baa2/BBB row not found")


def fetch_tax_live() -> dict:
    r = _get(OECD_API_URL, Accept="application/vnd.sdmx.data+csv; charset=utf-8")
    d = pd.read_csv(io.StringIO(r.text)).dropna(subset=["OBS_VALUE"])
    d = d.sort_values("TIME_PERIOD").groupby("REF_AREA").tail(1)
    out = pd.DataFrame({"iso3": d["REF_AREA"], "country": d["Reference area"],
                        "rate": d["OBS_VALUE"].astype(float) / 100,
                        "year": d["TIME_PERIOD"].astype(int)}).reset_index(drop=True)
    if "USA" not in set(out["iso3"]):
        raise ValueError("OECD: U.S. rate missing")
    return {"table": out, "as_of": str(int(out["year"].max()))}


def fetch_yields_live() -> dict:
    errors = []
    for url in TE_TABLE_URLS:
        try:
            page = _get(url).text
            best = max(pd.read_html(io.StringIO(page)), key=len)
            rows = []
            for _, row in best.iterrows():
                name = str(row.iloc[0]).strip()
                iso = TE_NAME_TO_ISO.get(name.lower())
                last = pd.to_numeric(row.iloc[1], errors="coerce")
                if iso and pd.notna(last):
                    rows.append({"iso3": iso, "yield": float(last) / 100,
                                 "previous": float(pd.to_numeric(row.iloc[2], errors="coerce")) / 100,
                                 "reference": str(row.iloc[3])})
            out = pd.DataFrame(rows).drop_duplicates("iso3")
            if "USA" not in set(out.get("iso3", [])) or len(out) < 10:
                raise ValueError("table incomplete")
            m = re.search(r"updated on \w+,\s*(\w+ \d{1,2}, \d{4})", page)
            return {"table": out, "as_of": m.group(1) if m else datetime.now().strftime("%B %d, %Y"),
                    "url": url}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    raise RuntimeError(" | ".join(errors))


LIVE_FETCHERS = {"betas": fetch_betas_live, "spread": fetch_spread_live,
                 "tax": fetch_tax_live, "yields": fetch_yields_live}


# ----------------------------------------------------------------------------
# FALLBACK (backup JSON written by `python build_backup.py`)
# ----------------------------------------------------------------------------
def to_jsonable(payload: dict) -> dict:
    return {k: (v.to_dict(orient="records") if isinstance(v, pd.DataFrame) else v)
            for k, v in payload.items()}


def from_backup(name: str) -> dict:
    snap = json.loads(BACKUP_PATH.read_text())
    item = dict(snap[name])
    if "table" in item:
        item["table"] = pd.DataFrame(item["table"])
    item["saved_on"] = snap.get("created", "unknown")
    return item


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def load_source(name: str) -> dict:
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        data = LIVE_FETCHERS[name]()
        data.update(mode="live", fetched_at=fetched)
    except Exception as exc:  # noqa: BLE001
        data = from_backup(name)
        data.update(mode="backup", fetched_at=fetched, error=str(exc)[:300])
    return data


# ----------------------------------------------------------------------------
# COMPUTE (pure math, no Streamlit)
# ----------------------------------------------------------------------------
def compute(beta_u: float, tax: float, erp: float, debt_share: float,
            y_us: float, y_f: float, spread_us: float,
            spread_f_override: float | None = None) -> dict:
    """All rates as decimals (0.065 = 6.5%).

    USD:      beta_L = beta_U * (1 + (1 - t) * D/E)
              k_E = y_US + beta_L * ERP            k_D = y_US + s_US
              WACC = E/V * k_E + D/V * k_D * (1 - t)
    Foreign:  (1 + k_FC) = (1 + k_USD) * (1 + y_F) / (1 + y_US)   (applied to k_E and k_D)
              => auto foreign spread s_F = k_D,FC - y_F = s_US * (1 + y_F) / (1 + y_US)
              k_D,FC = y_F + s_F  (s_F may be overridden)
              WACC_FC = E/V * k_E,FC + D/V * k_D,FC * (1 - t)
    """
    wd = debt_share
    we = 1.0 - wd
    de = wd / we
    beta_l = beta_u * (1 + (1 - tax) * de)

    ke_usd = y_us + beta_l * erp
    kd_usd = y_us + spread_us
    kd_usd_at = kd_usd * (1 - tax)
    wacc_usd = we * ke_usd + wd * kd_usd_at

    parity = (1 + y_f) / (1 + y_us)
    ke_f = (1 + ke_usd) * parity - 1
    spread_f_auto = (1 + kd_usd) * parity - 1 - y_f
    spread_f = spread_f_auto if spread_f_override is None else spread_f_override
    kd_f = y_f + spread_f
    kd_f_at = kd_f * (1 - tax)
    wacc_f = we * ke_f + wd * kd_f_at

    return dict(beta_u=beta_u, tax=tax, erp=erp, debt_share=wd, equity_share=we, de_ratio=de,
                beta_l=beta_l, parity=parity,
                y_us=y_us, y_f=y_f, spread_us=spread_us,
                spread_f_auto=spread_f_auto, spread_f=spread_f,
                spread_f_overridden=spread_f_override is not None,
                ke_usd=ke_usd, kd_usd=kd_usd, kd_usd_at=kd_usd_at, wacc_usd=wacc_usd,
                ke_f=ke_f, kd_f=kd_f, kd_f_at=kd_f_at, wacc_f=wacc_f)


def self_checks(base: dict) -> list[tuple[str, bool, str]]:
    """The three verification tests required by the assignment, run on live inputs."""
    args = dict(beta_u=base["beta_u"], tax=base["tax"], erp=base["erp"],
                debt_share=base["debt_share"], y_us=base["y_us"], spread_us=base["spread_us"])
    pairs = [("ke_usd", "ke_f"), ("kd_usd", "kd_f"), ("kd_usd_at", "kd_f_at"), ("wacc_usd", "wacc_f")]
    usd_keys = [p[0] for p in pairs]
    out = []

    eq = compute(**args, y_f=base["y_us"])
    gap = max(abs(eq[u] - eq[f]) for u, f in pairs)
    out.append(("Equal bond yields ⇒ identical USD and local results (spread override off)",
                gap < 1e-12, f"largest difference = {gap:.1e}"))

    z = compute(**{**args, "debt_share": 0.0}, y_f=base["y_f"])
    g0 = max(abs(z["wacc_usd"] - z["ke_usd"]), abs(z["wacc_f"] - z["ke_f"]))
    out.append(("Zero debt ⇒ WACC equals cost of equity (both currencies)",
                g0 < 1e-12, f"WACC_USD {z['wacc_usd']:.4%} = k_E {z['ke_usd']:.4%}; "
                            f"WACC_FC {z['wacc_f']:.4%} = k_E {z['ke_f']:.4%}"))

    a = compute(**args, y_f=base["y_f"])
    b = compute(**args, y_f=base["y_f"], spread_f_override=a["spread_f_auto"] + 0.03)
    gu = max(abs(a[k] - b[k]) for k in usd_keys + ["beta_l", "spread_us"])
    out.append(("Overriding the foreign spread (+3 pp) leaves USD results unchanged",
                gu < 1e-15 and b["wacc_f"] != a["wacc_f"],
                f"USD change = {gu:.1e}; local WACC moved {a['wacc_f']:.2%} → {b['wacc_f']:.2%}"))
    return out


# ----------------------------------------------------------------------------
# VERDICT (plain English, rule-based)
# ----------------------------------------------------------------------------
def turbulence(gap_pp: float) -> tuple[str, str]:
    for bound, label, color in TURBULENCE:
        if abs(gap_pp) < bound:
            return label, color
    return TURBULENCE[-1][1], TURBULENCE[-1][2]


def verdict(r: dict, ind: str, iso: str, tax_us: float, tax_f: float | None,
            ref: str, debt_pct: float) -> tuple[str, list[str], list[str]]:
    c = COUNTRIES[iso]
    cur, name = c["cur"], c["name"]
    gap = (r["wacc_f"] - r["wacc_usd"]) * 100
    if iso == "USA":
        meaning = (f"You are flying domestic. A **{ind}** project in the U.S. must earn at least "
                   f"**{r['wacc_usd']:.2%} a year** to create value. Pick a destination country to "
                   f"see the same project priced in another currency.")
    elif abs(gap) < 0.05:
        meaning = (f"A **{ind}** project in **{name}** needs about **{r['wacc_f']:.2%}** in {cur}, "
                   f"the same as **{r['wacc_usd']:.2%}** in USD: both bond markets expect similar "
                   f"inflation, so the currency does not change the hurdle.")
    else:
        direction = "lose" if gap > 0 else "gain"
        meaning = (f"A **{ind}** project in **{name}** must earn **{r['wacc_f']:.2%} a year in {cur}**, "
                   f"or **{r['wacc_usd']:.2%} in USD**. Same project, same risk: the "
                   f"**{gap:+.2f} pp** gap is the bond market's expectation that the {cur} will "
                   f"{direction} value against the dollar (mostly an inflation gap), not extra risk.")

    opps, risks = [], []
    if tax_f is not None and iso != "USA" and tax_f < tax_us - 0.01:
        opps.append(f"Lower corporate tax than the U.S. ({tax_f:.1%} vs {tax_us:.1%}): the project "
                    f"keeps more of each unit of profit (a cash-flow benefit; WACC alone doesn't show it).")
    if iso != "USA" and r["y_f"] < r["y_us"]:
        opps.append(f"Local 10-year rates ({r['y_f']:.2%}) are below U.S. rates: {cur} borrowing "
                    f"and {cur} hurdle rates are cheaper.")
    if r["beta_u"] < 0.8:
        opps.append(f"Defensive industry (unlevered beta {r['beta_u']:.2f}): equity investors ask "
                    f"for less, wherever the project is.")
    if debt_pct < 40:
        opps.append("Debt is cheaper than equity after tax: a bit more debt lowers WACC through "
                    "the tax shield (try the debt slider) — as long as the BBB rating holds.")
    if iso == "USA":
        opps.append("Use the world map tab to spot which currencies give a lower or higher hurdle.")

    if gap > 3:
        risks.append(f"High local hurdle: {cur} cash flows must grow about {gap:.1f} pp faster each "
                     f"year than USD cash flows just to break even.")
    if r["y_f"] >= 0.10:
        risks.append(f"Double-digit sovereign yield ({r['y_f']:.2%}): signals high inflation and/or "
                     f"country risk. This model adds no separate country-risk premium.")
    if r["beta_u"] > 1.2:
        risks.append(f"High-beta industry (unlevered beta {r['beta_u']:.2f}): equity is expensive "
                     f"and returns swing hard with the market.")
    if iso != "USA" and tax_f is not None and tax_f > tax_us + 0.01:
        risks.append(f"Higher corporate tax than the U.S. ({tax_f:.1%} vs {tax_us:.1%}).")
    if ref and not re.search(datetime.now().strftime("%b/%y"), ref, re.I) and iso != "USA":
        risks.append(f"The {name} bond quote is dated {ref} — it may be stale; consider a manual override.")
    if iso != "USA":
        risks.append(f"Assumes {cur} borrowing costs the U.S. BBB spread scaled by parity; real local "
                     f"loans may cost more (override the local spread to test it).")
    return meaning, opps[:3], risks[:3]


# ----------------------------------------------------------------------------
# CHARTS
# ----------------------------------------------------------------------------
def world_map(df: pd.DataFrame, selected: str, cur_title: str) -> go.Figure:
    fig = go.Figure(go.Choropleth(
        locations=df["iso3"], z=df["wacc_f"] * 100, text=df["label"],
        customdata=np.stack([df["wacc_usd"] * 100, df["y_f"] * 100, df["cur"]], axis=-1),
        hovertemplate=("<b>%{text}</b><br>Local-currency WACC: %{z:.2f}%"
                       "<br>USD WACC: %{customdata[0]:.2f}%<br>10Y yield: %{customdata[1]:.2f}%"
                       "<extra></extra>"),
        colorscale=[[0, "#2BA84A"], [0.35, "#F7E08A"], [0.7, "#F2A900"], [1, "#E4572E"]],
        marker_line_color="white", marker_line_width=0.6,
        zmin=float(np.nanpercentile(df["wacc_f"] * 100, 5)),
        zmax=float(np.nanpercentile(df["wacc_f"] * 100, 92)),   # outliers (e.g. Turkey) saturate red
        colorbar=dict(title=dict(text="WACC<br>local %"), thickness=12, len=0.75),
    ))
    if selected in set(df["iso3"]):
        fig.add_trace(go.Choropleth(locations=[selected], z=[0], showscale=False, hoverinfo="skip",
                                    colorscale=[[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,0)"]],
                                    marker_line_color=NAVY, marker_line_width=3))
    # "Airports": one clickable dot per country (Streamlit selections work on point traces).
    fig.add_trace(go.Scattergeo(
        locations=df["iso3"], locationmode="ISO-3", customdata=np.stack([df["iso3"]], axis=-1),
        text=df["label"], mode="markers", showlegend=False,
        marker=dict(size=np.where(df["iso3"] == selected, 14, 8), color=NAVY,
                    symbol=np.where(df["iso3"] == selected, "star", "circle"),
                    line=dict(color="white", width=1)),
        hovertemplate="<b>%{text}</b><br>✈ click to fly here<extra></extra>"))
    fig.update_geos(showframe=False, showcoastlines=False, projection_type="natural earth",
                    bgcolor="rgba(0,0,0,0)", showland=True, landcolor="#E9E4D8",
                    showcountries=True, countrycolor="white", lataxis_range=[-58, 85])
    fig.update_layout(height=470, margin=dict(l=0, r=0, t=10, b=0), title=None,
                      paper_bgcolor="rgba(0,0,0,0)", clickmode="event+select",
                      font=dict(family="Space Grotesk, sans-serif", color=NAVY))
    return fig


def industry_dumbbell(df: pd.DataFrame, selected: str, cur: str) -> go.Figure:
    df = df.sort_values("wacc_f")
    fig = go.Figure()
    for _, r in df.iterrows():
        fig.add_trace(go.Scatter(x=[r.wacc_usd * 100, r.wacc_f * 100], y=[r.industry] * 2,
                                 mode="lines", line=dict(color="#CFC8B8", width=2),
                                 hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=df.wacc_usd * 100, y=df.industry, mode="markers", name="WACC USD",
                             marker=dict(color=SKY, size=8),
                             hovertemplate="%{y}<br>USD WACC %{x:.2f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=df.wacc_f * 100, y=df.industry, mode="markers", name=f"WACC {cur}",
                             marker=dict(color=CORAL, size=8),
                             hovertemplate="%{y}<br>" + cur + " WACC %{x:.2f}%<extra></extra>"))
    sel = df[df.industry == selected]
    if len(sel):
        fig.add_trace(go.Scatter(x=sel.wacc_f * 100, y=sel.industry, mode="markers+text",
                                 name="Your industry", text=["  ◀ you are here"],
                                 textposition="middle right",
                                 marker=dict(color=NAVY, size=15, symbol="star")))
    fig.update_layout(height=max(500, 19 * len(df)), margin=dict(l=10, r=30, t=90, b=10),
                      xaxis=dict(title="WACC (%)", side="top", gridcolor="#E9E4D8"),
                      yaxis=dict(tickfont=dict(size=10)), plot_bgcolor="white",
                      paper_bgcolor="rgba(0,0,0,0)", legend=dict(orientation="h", yref="container", y=0.995, yanchor="top", x=0),
                      font=dict(family="Space Grotesk, sans-serif", color=NAVY))
    return fig


# ----------------------------------------------------------------------------
# UI HELPERS
# ----------------------------------------------------------------------------
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@500;700&display=swap');
.stApp, .stMarkdown, .vcard, p, li, label, input, textarea { font-family: 'Space Grotesk', sans-serif; }
.block-container { padding-top: 2.2rem; max-width: 1180px; }
h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; color: #0B1F3A; letter-spacing: -0.02em; }
.hero { display:flex; align-items:flex-end; justify-content:space-between; flex-wrap:wrap; gap:8px;
        border-bottom: 2px dashed #0B1F3A33; padding-bottom: 10px; margin-bottom: 14px; }
.hero h1 { margin:0; font-size: 2.3rem; }
.hero .sub { color:#4A5A70; font-size:1.02rem; }
.hero .tag { font-family:'JetBrains Mono', monospace; font-size:.78rem; background:#0B1F3A; color:#FFC72C;
             padding:4px 10px; border-radius:20px; }
[data-testid="stHeaderActionElements"] { display:none; }
.step { font-family:'JetBrains Mono', monospace; font-size:.78rem; color:#E4572E; letter-spacing:.12em; }

/* ---------- boarding pass ---------- */
.bp { display:flex; flex-wrap:wrap; background:#FFF8EC; border-radius:18px; overflow:hidden;
      box-shadow: 0 10px 30px rgba(11,31,58,.14); margin: 6px 0 22px; border:1px solid #EADFCB; }
.bp-main { flex: 3 1 520px; padding: 18px 26px 20px; }
.bp-stub { flex: 1 1 220px; padding: 18px 22px; background:#0B1F3A; color:#FFF8EC; position:relative;
           display:flex; flex-direction:column; justify-content:space-between; gap:10px; }
.bp-stub:before { content:''; position:absolute; left:-1px; top:0; bottom:0; width:2px;
  background-image: linear-gradient(#FFF8EC 50%, transparent 50%); background-size: 2px 14px; }
.bp-head { display:flex; justify-content:space-between; font-family:'JetBrains Mono', monospace;
           font-size:.74rem; letter-spacing:.14em; color:#E4572E; }
.bp-route { display:flex; align-items:center; justify-content:space-between; margin: 10px 0 14px; gap:8px; }
.bp-city .code { font-size: 3.1rem; font-weight:700; line-height:1; color:#0B1F3A; }
.bp-city .name { font-size: 1.02rem; font-weight:500; }
.bp-city .lab { font-family:'JetBrains Mono', monospace; font-size:.66rem; color:#7A879A; letter-spacing:.12em; }
.bp-plane { flex:1; text-align:center; color:#3E7CB1; font-size:1.5rem; white-space:nowrap; overflow:hidden; }
.bp-plane span { border-top: 2px dashed #3E7CB1; display:inline-block; width:38%; vertical-align:middle; }
.bp-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px 18px;
           border-top: 1px solid #EADFCB; padding-top: 12px; }
.bp-f .k { font-family:'JetBrains Mono', monospace; font-size:.64rem; color:#7A879A; letter-spacing:.14em; }
.bp-f .v { font-weight:700; font-size:1.02rem; color:#0B1F3A; }
.bp-stub .k { font-family:'JetBrains Mono', monospace; font-size:.66rem; color:#9FB3CC; letter-spacing:.14em; }
.bp-stub .big { font-family:'JetBrains Mono', monospace; font-size:2.2rem; font-weight:700; color:#FFC72C; line-height:1.05; }
.chip { display:inline-block; padding:4px 10px; border-radius:20px; font-family:'JetBrains Mono', monospace;
        font-size:.72rem; font-weight:700; color:#0B1F3A; }
.barcode { height:38px; border-radius:3px; background: repeating-linear-gradient(90deg,#FFF8EC 0 2px,
           transparent 2px 4px,#FFF8EC 4px 5px,transparent 5px 8px,#FFF8EC 8px 11px,transparent 11px 13px); opacity:.85; }

/* ---------- departure board ---------- */
.board { background:#111418; border-radius:14px; padding: 16px 18px 12px; overflow-x:auto;
         box-shadow: inset 0 0 0 2px #23272E, 0 10px 26px rgba(0,0,0,.25); margin-bottom: 18px; }
.board-title { display:flex; justify-content:space-between; color:#FFC72C; font-family:'JetBrains Mono', monospace;
               letter-spacing:.2em; font-size:.82rem; margin-bottom:8px; }
.board table { width:100%; border-collapse:separate; border-spacing:0 5px; min-width: 560px; }
.board th { color:#8C96A3; font-family:'JetBrains Mono', monospace; font-size:.68rem; letter-spacing:.16em;
            text-align:left; font-weight:500; padding: 0 6px; }
.board td { color:#E8ECF1; font-family:'JetBrains Mono', monospace; font-size:.86rem; padding: 2px 6px; white-space:nowrap; }
.board tr.wacc td { border-top: 1px dashed #FFC72C55; padding-top:8px; }
.board tr.wacc td.lbl { color:#FFC72C; font-weight:700; }
.fl { display:inline-block; background:#1E232B; color:#FFC72C; margin-right:2px; padding: 2px 3px;
      border-radius:3px; min-width: .9em; text-align:center; font-weight:700;
      box-shadow: inset 0 -1px 0 #000, inset 0 1px 0 #2C333D;
      animation: flip .5s ease-out both; transform-origin: 50% 50%; }
tr.wacc .fl { font-size:1.05rem; background:#2A2410; }
.gap-pos { color:#FF8A6B !important; } .gap-neg { color:#6EE7A0 !important; }
@keyframes flip { 0% { transform: rotateX(90deg); opacity:.2; } 60% { transform: rotateX(-15deg);} 100% { transform: rotateX(0); opacity:1; } }

/* ---------- verdict cards ---------- */
.vcard { border-radius:14px; padding: 14px 16px; height:100%; border:1px solid #EADFCB; background:white; }
.vcard h4 { margin: 0 0 6px; font-size: 1rem; }
.vcard ul { margin: 0; padding-left: 1.1rem; } .vcard li { margin-bottom: 4px; font-size:.93rem; }
.v-mean { border-left: 6px solid #3E7CB1; } .v-opp { border-left: 6px solid #2BA84A; } .v-risk { border-left: 6px solid #E4572E; }
.pill { display:inline-block; padding:2px 9px; border-radius:12px; font-size:.75rem; font-weight:600; margin-right:4px; }
.pill.live { background:#DFF5E6; color:#17692F; } .pill.backup { background:#FFF1C9; color:#7A5A00; } .pill.manual { background:#E8E0FF; color:#4B2E9E; }
</style>
"""


def pct(x: float, d: int = 2) -> str:
    return f"{x * 100:.{d}f}%"


def flaps(text: str, delay: float) -> str:
    return "".join(f'<span class="fl" style="animation-delay:{delay + i * 0.035:.2f}s">'
                   f'{"&nbsp;" if ch == " " else html.escape(ch)}</span>' for i, ch in enumerate(text))


def board_html(r: dict, cur: str, f_flag: str) -> str:
    rows = [
        ("10Y GOV'T BOND (RISK-FREE)", r["y_us"], r["y_f"], True),
        ("LEVERED BETA", r["beta_l"], r["beta_l"], False),
        ("COST OF EQUITY", r["ke_usd"], r["ke_f"], True),
        ("BBB SPREAD" + (" ✎" if r["spread_f_overridden"] else ""), r["spread_us"], r["spread_f"], True),
        ("COST OF DEBT · PRE-TAX", r["kd_usd"], r["kd_f"], True),
        ("COST OF DEBT · AFTER TAX", r["kd_usd_at"], r["kd_f_at"], True),
        ("WACC", r["wacc_usd"], r["wacc_f"], True),
    ]
    body = []
    for i, (lbl, u, f, is_pct) in enumerate(rows):
        su = f"{u * 100:6.2f}%" if is_pct else f"{u:6.2f} "
        sf = f"{f * 100:6.2f}%" if is_pct else f"{f:6.2f} "
        if is_pct:
            g = (f - u) * 100
            cls = "gap-pos" if g > 0.005 else ("gap-neg" if g < -0.005 else "")
            gtxt = f"{g:+.2f} pp"
        else:
            cls, gtxt = "", "same"
        trc = ' class="wacc"' if lbl == "WACC" else ""
        body.append(f'<tr{trc}><td class="lbl">{lbl}</td><td>{flaps(su.strip(), 0.05 * i)}</td>'
                    f'<td>{flaps(sf.strip(), 0.05 * i + 0.15)}</td><td class="{cls}">{gtxt}</td></tr>')
    return (f'<div class="board"><div class="board-title"><span>✈ DEPARTURES · HURDLE RATES</span>'
            f'<span>{datetime.now().strftime("%d %b %Y").upper()}</span></div><table>'
            f'<tr><th>ITEM</th><th>🇺🇸 USD</th><th>{f_flag} {cur}</th><th>GAP</th></tr>'
            + "".join(body) + "</table></div>")


def boarding_pass_html(iso: str, ind: str, r: dict, tax_label: str, firms: int | None) -> str:
    c = COUNTRIES[iso]
    gap = (r["wacc_f"] - r["wacc_usd"]) * 100
    gap = 0.0 if abs(gap) < 0.005 else gap   # avoid "-0.00"
    label, color = turbulence(gap)
    fields = [
        ("PASSENGER · INDUSTRY", html.escape(ind)),
        ("β UNLEVERED → LEVERED", f"{r['beta_u']:.2f} → {r['beta_l']:.2f}"),
        ("PROJECT TAX", tax_label),
        ("EQUITY RISK PREMIUM", pct(r["erp"], 1)),
        ("DEBT / CAPITAL", f"{r['debt_share'] * 100:.0f}%  (D/E {r['de_ratio']:.2f})"),
        ("FIRMS IN SAMPLE", f"{firms:,}" if firms else "—"),
    ]
    grid = "".join(f'<div class="bp-f"><div class="k">{k}</div><div class="v">{v}</div></div>'
                   for k, v in fields)
    return f"""
<div class="bp">
  <div class="bp-main">
    <div class="bp-head"><span>✈ CAPITAL AIR · BOARDING PASS</span><span>{datetime.now().strftime('%d %b %Y').upper()}</span></div>
    <div class="bp-route">
      <div class="bp-city"><div class="code">USD</div><div class="name">🇺🇸 United States</div><div class="lab">FROM · HOME CURRENCY</div></div>
      <div class="bp-plane"><span></span> ✈ <span></span></div>
      <div class="bp-city" style="text-align:right"><div class="code">{c['cur']}</div>
        <div class="name">{flag(iso)} {html.escape(c['name'])}</div><div class="lab">TO · PROJECT CURRENCY</div></div>
    </div>
    <div class="bp-grid">{grid}</div>
  </div>
  <div class="bp-stub">
    <div><div class="k">HURDLE RATE · WACC IN {c['cur']}</div><div class="big">{pct(r['wacc_f'])}</div>
         <div class="k" style="margin-top:4px">IN USD: {pct(r['wacc_usd'])}</div></div>
    <div><div class="k">FX TURBULENCE</div><span class="chip" style="background:{color}">● {label} · {gap:+.2f} PP</span></div>
    <div class="barcode"></div>
  </div>
</div>"""


def source_pill(mode: str) -> str:
    return f'<span class="pill {mode}">{mode.upper()}</span>'


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
def _on_map_select() -> None:
    ev = st.session_state.get("world_map") or {}
    pts = (ev.get("selection") or {}).get("points") or []
    if pts:
        p = pts[0]
        loc = p.get("location") or (p.get("customdata") or [None])[0]
        if loc in st.session_state.get("_available", []):
            st.session_state["country"] = loc


def main() -> None:
    st.set_page_config(page_title="Capital Boarding Pass · International WACC", page_icon="✈️",
                       layout="wide", initial_sidebar_state="auto")
    st.markdown(CSS, unsafe_allow_html=True)

    with st.spinner("Checking in: fetching betas, spreads, tax rates and bond yields…"):
        src = {k: load_source(k) for k in LIVE_FETCHERS}
    betas, spread, tax_src, ylds = src["betas"], src["spread"], src["tax"], src["yields"]

    tax_map = dict(zip(tax_src["table"]["iso3"], tax_src["table"]["rate"]))
    tax_year = dict(zip(tax_src["table"]["iso3"], tax_src["table"]["year"]))
    ytab = ylds["table"].set_index("iso3")
    available = [i for i in COUNTRIES if i in ytab.index and i in tax_map]
    available = ["USA"] + sorted([i for i in available if i != "USA"], key=lambda i: COUNTRIES[i]["name"])
    st.session_state["_available"] = available
    industries = betas["table"]["industry"].tolist()

    # ---------------- hero ----------------
    st.markdown(
        '<div class="hero"><div><h1>✈️ Capital Boarding Pass</h1>'
        '<div class="sub">What return must a project earn — in dollars and in local currency? '
        'Pick an industry and a destination; we price the trip.</div></div>'
        '<span class="tag">INTERNATIONAL COST OF CAPITAL</span></div>', unsafe_allow_html=True)

    backups = [k for k, v in src.items() if v["mode"] == "backup"]
    if backups:
        st.warning("⚠️ Live data unavailable for: **" + ", ".join(backups) + "**. Using the saved "
                   "snapshot (see *Data sources* tab for dates). Press **Refresh data** to retry.")

    # ---------------- step 1: flight plan ----------------
    st.markdown('<div class="step">STEP 1 · PLAN YOUR FLIGHT</div>', unsafe_allow_html=True)
    if "country" not in st.session_state:
        st.session_state["country"] = DEFAULT_COUNTRY
    if st.session_state["country"] not in available:
        st.session_state["country"] = DEFAULT_COUNTRY

    c1, c2 = st.columns([1.15, 1])
    with c1:
        industry = st.selectbox("🏭 Industry (Damodaran, U.S. firms)", industries,
                                index=industries.index(DEFAULT_INDUSTRY) if DEFAULT_INDUSTRY in industries else 0,
                                key="industry")
    with c2:
        iso = st.selectbox("🌍 Destination country (project currency)", available,
                           format_func=lambda i: f"{flag(i)}  {COUNTRIES[i]['name']} · {COUNTRIES[i]['cur']}",
                           key="country")
    c3, c4, c5 = st.columns([1, 1, 1.1])
    with c3:
        erp = st.slider("📈 Equity risk premium (%)", 3.0, 10.0, DEFAULT_ERP, 0.1,
                        help="Extra return investors demand to hold stocks instead of U.S. Treasuries.") / 100
    with c4:
        debt = st.slider("🏦 Target debt share D/(D+E) (%)", 0, 90, DEFAULT_DEBT, 1,
                         help="Share of the project financed with debt. 0% = all equity.")
    with c5:
        cname = COUNTRIES[iso]["name"]
        tax_choice = st.segmented_control(
            "🧾 Project tax rate (applied in both currencies)", ["US", "FOREIGN"], default="US",
            required=True, key="tax_choice",
            format_func=lambda o: "🇺🇸 U.S. rate" if o == "US" else f"{flag(iso)} {cname} rate")

    # ---------------- sidebar: overrides + refresh ----------------
    beta_row = betas["table"].set_index("industry").loc[industry]
    with st.sidebar:
        st.markdown("### 🛫 Control tower")
        if st.button("🔄 Refresh data", type="primary", width="stretch",
                     help="Clear the cache and fetch all four sources again."):
            load_source.clear()
            st.rerun()
        st.caption("Status: " + " ".join(f"{k} {source_pill(v['mode'])}" for k, v in src.items()),
                   unsafe_allow_html=True)
        st.caption(f"Last fetch: {src['yields']['fetched_at']}")
        st.markdown("### ✏️ Manual overrides")
        st.caption("Switch on to replace a fetched value. Off = live data.")

        def override(label: str, fetched: float, key: str, step: float = 0.01, unit: str = "%") -> tuple[float, bool]:
            on = st.toggle(label, key=f"t_{key}")
            if on:
                v = st.number_input(f"{label} ({unit})", value=round(fetched * (100 if unit == '%' else 1), 4),
                                    step=step, format="%.4f", key=f"n_{key}", label_visibility="collapsed")
                return (v / 100 if unit == "%" else v), True
            return fetched, False

        beta_u, ov_beta = override(f"Unlevered beta · {industry[:26]}", float(beta_row["beta_u"]),
                                   f"beta_{industry}", step=0.01, unit="x")
        tax_us, ov_tus = override("U.S. corporate tax rate", float(tax_map["USA"]), "tax_us")
        if iso != "USA":
            tax_f, ov_tf = override(f"{cname} corporate tax rate", float(tax_map[iso]), f"tax_{iso}")
        else:
            tax_f, ov_tf = tax_us, ov_tus
        spread_us, ov_sp = override("USD BBB spread", float(spread["value"]), "spread_us")
        y_us, ov_yus = override("U.S. 10Y yield", float(ytab.loc["USA", "yield"]), "y_us")
        if iso != "USA":
            y_f, ov_yf = override(f"{cname} 10Y yield", float(ytab.loc[iso, "yield"]), f"y_{iso}")
        else:
            y_f, ov_yf = y_us, ov_yus

        # foreign spread: auto unless overridden
        auto_preview = compute(beta_u, tax_us, erp, debt / 100, y_us, y_f, spread_us)["spread_f_auto"]
        ov_sf = st.toggle(f"{COUNTRIES[iso]['cur']} BBB spread (foreign)", key=f"t_sf_{iso}")
        if ov_sf:
            spread_f_ov = st.number_input("foreign spread (%)", value=round(auto_preview * 100, 4), step=0.05,
                                          format="%.4f", key=f"n_sf_{iso}", label_visibility="collapsed") / 100
        else:
            spread_f_ov = None
            st.caption(f"Auto: {pct(auto_preview, 3)} = USD spread × (1+y_local)/(1+y_US)")
        st.divider()
        st.caption("Educational tool for an MBA International Finance course — not investment advice.")

    project_tax = tax_us if (tax_choice == "US" or iso == "USA") else tax_f
    r = compute(beta_u, project_tax, erp, debt / 100, y_us, y_f, spread_us, spread_f_ov)
    cur = COUNTRIES[iso]["cur"]
    loc = f"{cur} (local)"   # distinct column label even when the destination is the U.S.
    tax_label = (f"🇺🇸 U.S. {pct(project_tax, 1)}" if (tax_choice == "US" or iso == "USA")
                 else f"{flag(iso)} {cname} {pct(project_tax, 1)}")
    ref = str(ytab.loc[iso, "reference"]) if iso in ytab.index else ""

    # ---------------- boarding pass + board ----------------
    st.markdown('<div class="step">STEP 2 · YOUR BOARDING PASS</div>', unsafe_allow_html=True)
    st.markdown(boarding_pass_html(iso, industry, r, tax_label, int(beta_row["firms"])
                                   if pd.notna(beta_row["firms"]) else None), unsafe_allow_html=True)
    st.markdown(board_html(r, cur, flag(iso)), unsafe_allow_html=True)
    manual = [n for n, f in [("unlevered beta", ov_beta), ("U.S. tax", ov_tus), ("foreign tax", ov_tf and iso != "USA"),
                             ("USD spread", ov_sp), ("U.S. yield", ov_yus), ("foreign yield", ov_yf and iso != "USA"),
                             ("foreign spread", ov_sf)] if f]
    if manual:
        st.info("✏️ Manual inputs in use: **" + ", ".join(manual) + "**.")

    # ---------------- verdict ----------------
    st.markdown('<div class="step">STEP 3 · WHAT DOES IT MEAN? (PLAIN ENGLISH)</div>', unsafe_allow_html=True)
    meaning, opps, risks = verdict(r, industry, iso, tax_us, tax_f if iso != "USA" else None, ref, debt)
    md = lambda s: re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(s, quote=False))
    v1, v2, v3 = st.columns([1.3, 1, 1])
    v1.markdown(f'<div class="vcard v-mean"><h4>🧭 Where you stand</h4>{md(meaning)}</div>', unsafe_allow_html=True)
    v2.markdown('<div class="vcard v-opp"><h4>🟢 Opportunities</h4><ul>' +
                "".join(f"<li>{md(o)}</li>" for o in opps) + "</ul></div>", unsafe_allow_html=True)
    v3.markdown('<div class="vcard v-risk"><h4>🔴 Watch-outs</h4><ul>' +
                "".join(f"<li>{md(o)}</li>" for o in (risks or ['Nothing unusual flagged.'])) + "</ul></div>",
                unsafe_allow_html=True)
    st.write("")

    # ---------------- tabs ----------------
    st.markdown('<div class="step">STEP 4 · EXPLORE</div>', unsafe_allow_html=True)
    t_map, t_ind, t_math, t_src, t_chk, t_dl = st.tabs(
        ["🌍 World map", "🏭 All industries", "🧮 How it's calculated", "📡 Data sources",
         "✅ Self-checks", "📥 Download"])

    # world map: same industry and settings, every country's own yield (and own tax if FOREIGN)
    rows = []
    for i in available:
        t_i = tax_us if (tax_choice == "US" or i == "USA") else (tax_f if i == iso else float(tax_map[i]))
        y_i = y_f if i == iso else float(ytab.loc[i, "yield"])
        res = compute(beta_u, t_i, erp, debt / 100, y_us, y_i, spread_us, spread_f_ov if i == iso else None)
        rows.append({"iso3": i, "label": f"{flag(i)} {COUNTRIES[i]['name']} ({COUNTRIES[i]['cur']})",
                     "cur": COUNTRIES[i]["cur"], "y_f": y_i, "tax": t_i,
                     "wacc_usd": res["wacc_usd"], "wacc_f": res["wacc_f"], "ke_f": res["ke_f"]})
    mdf = pd.DataFrame(rows)
    with t_map:
        st.markdown(f"**WACC in local currency for _{industry}_, every country** — click a ✈ dot to fly there. "
                    f"Tax: {'U.S. rate everywhere' if tax_choice == 'US' else 'each country’s own rate'}.")
        st.plotly_chart(world_map(mdf, iso, cur), key="world_map", on_select=_on_map_select,
                        selection_mode="points", config={"displayModeBar": False})
        lo, hi = st.columns(2)
        fmt = lambda d: "\n".join(f"{n+1}. {row.label} — **{pct(row.wacc_f)}**" for n, row in enumerate(d.itertuples()))
        lo.markdown("🟢 **Lowest local hurdle**\n\n" + fmt(mdf.nsmallest(5, "wacc_f")))
        hi.markdown("🔴 **Highest local hurdle**\n\n" + fmt(mdf.nlargest(5, "wacc_f")))
        st.caption("Small territories (e.g. Singapore, Hong Kong) are easier to pick from the country menu.")

    # all industries
    irows = []
    for _, b in betas["table"].iterrows():
        res = compute(float(b.beta_u) if b.industry != industry else beta_u, project_tax, erp, debt / 100,
                      y_us, y_f, spread_us, spread_f_ov)
        irows.append({"industry": b.industry, "firms": b.firms, "beta_u": res["beta_u"], "beta_l": res["beta_l"],
                      "ke_usd": res["ke_usd"], "ke_f": res["ke_f"], "kd_usd_at": res["kd_usd_at"],
                      "kd_f_at": res["kd_f_at"], "wacc_usd": res["wacc_usd"], "wacc_f": res["wacc_f"]})
    idf = pd.DataFrame(irows)
    with t_ind:
        st.markdown(f"**Every Damodaran industry flown to {flag(iso)} {cname}** with the same tax, ERP, debt share "
                    f"and bond yields. Only the beta changes.")
        plot_df = idf[~idf.industry.str.startswith("Total Market")]
        st.plotly_chart(industry_dumbbell(plot_df, industry, cur), config={"displayModeBar": False})
        show = idf.rename(columns={"industry": "Industry", "firms": "Firms", "beta_u": "β unlevered",
                                   "beta_l": "β levered", "ke_usd": "k_E USD", "ke_f": f"k_E {loc}",
                                   "kd_usd_at": "k_D after-tax USD", "kd_f_at": f"k_D after-tax {loc}",
                                   "wacc_usd": "WACC USD", "wacc_f": f"WACC {loc}"})
        pcols = [c for c in show.columns if c.startswith(("k_", "WACC"))]
        st.dataframe(show.style.format({**{c: "{:.2%}" for c in pcols}, "β unlevered": "{:.2f}",
                                        "β levered": "{:.2f}", "Firms": "{:,.0f}"})
                     .apply(lambda s: ["background-color:#FFF1C9" if v == industry else "" for v in show["Industry"]],
                            axis=0, subset=["Industry"]),
                     hide_index=True, height=420)

    with t_math:
        st.markdown("#### The recipe (all rates per year)")
        st.markdown(f"""
| Step | Formula | Your numbers |
|---|---|---|
| 1. Debt-to-equity | D/E = w_D / (1 − w_D) | {debt/100:.2f} / {1-debt/100:.2f} = **{r['de_ratio']:.4f}** |
| 2. Relever beta (Hamada) | β_L = β_U × [1 + (1 − t) × D/E] | {r['beta_u']:.4f} × [1 + (1 − {project_tax:.4f}) × {r['de_ratio']:.4f}] = **{r['beta_l']:.4f}** |
| 3. Cost of equity, USD | k_E = y_US + β_L × ERP | {pct(y_us,3)} + {r['beta_l']:.4f} × {pct(erp,2)} = **{pct(r['ke_usd'],3)}** |
| 4. Cost of debt, USD | k_D = y_US + BBB spread | {pct(y_us,3)} + {pct(spread_us,3)} = **{pct(r['kd_usd'],3)}** |
| 5. WACC, USD | w_E·k_E + w_D·k_D·(1 − t) | {1-debt/100:.2f}×{pct(r['ke_usd'],3)} + {debt/100:.2f}×{pct(r['kd_usd'],3)}×(1 − {project_tax:.4f}) = **{pct(r['wacc_usd'],3)}** |
| 6. Parity factor | (1 + y_{cur}) / (1 + y_US) | (1 + {pct(y_f,3)}) / (1 + {pct(y_us,3)}) = **{r['parity']:.6f}** |
| 7. Cost of equity, {cur} | (1 + k_E,USD) × factor − 1 | (1 + {pct(r['ke_usd'],3)}) × {r['parity']:.6f} − 1 = **{pct(r['ke_f'],3)}** |
| 8. {cur} spread | auto: s_US × factor{' (overridden)' if r['spread_f_overridden'] else ''} | {'manual → ' if r['spread_f_overridden'] else f"{pct(spread_us,3)} × {r['parity']:.6f} = "}**{pct(r['spread_f'],3)}** |
| 9. Cost of debt, {cur} | y_{cur} + {cur} spread | {pct(y_f,3)} + {pct(r['spread_f'],3)} = **{pct(r['kd_f'],3)}** |
| 10. WACC, {cur} | w_E·k_E,{cur} + w_D·k_D,{cur}·(1 − t) | {1-debt/100:.2f}×{pct(r['ke_f'],3)} + {debt/100:.2f}×{pct(r['kd_f'],3)}×(1 − {project_tax:.4f}) = **{pct(r['wacc_f'],3)}** |
""")
        st.markdown("#### Main assumptions, in plain words")
        st.markdown(f"""
- **Same project, two price tags.** Risk (beta), tax rate, ERP and debt share are identical in both columns; only the currency changes.
- **Why the currencies differ:** the 10-year bond yield gap is read as expected inflation / depreciation (*international Fisher effect*). The multiplicative parity factor turns any USD rate into a {cur} rate: (1 + r_{cur}) = (1 + r_USD) × (1 + y_{cur}) / (1 + y_US).
- **Beta comes from U.S. firms** (Damodaran's industry average *unlevered beta*), relevered to your debt share with the Hamada formula and the project tax rate.
- **Risk-free rate = 10-year government yield** of each country. No separate country-risk premium is added: if the sovereign yield already embeds default risk, the {cur} rate partly carries it.
- **Borrowing at BBB.** The project borrows like a BBB/Baa2 large non-financial U.S. company. The {cur} spread is the USD spread translated by parity unless you override it.
- **One tax rate** (U.S. or {cname}, your choice) relevers beta *and* sets the debt tax shield in both currencies.
- **Educational, not advice.** Real projects also need country-risk, liquidity and project-specific adjustments.
""")

    with t_src:
        def st_row(item, value, s, link, as_of):
            return {"Input": item, "Value used": value, "Status": s["mode"], "As of": as_of,
                    "Source": link, "Fetched": s["fetched_at"]}
        srows = [
            st_row(f"Unlevered beta · {industry}", f"{beta_u:.4f}" + (" (manual)" if ov_beta else ""), betas,
                   BETAS_URL, f"updated {betas['as_of']}"),
            st_row("USD BBB spread (Baa2/BBB, large non-financial)", pct(spread_us, 3) + (" (manual)" if ov_sp else ""),
                   spread, RATINGS_URL, spread["as_of"]),
            st_row("U.S. corporate tax (combined statutory)", pct(tax_us, 2) + (" (manual)" if ov_tus else ""),
                   tax_src, OECD_PAGE_URL, f"{tax_year.get('USA')} (OECD API: sdmx.oecd.org)"),
            st_row(f"{cname} corporate tax", pct(tax_f, 2) + (" (manual)" if ov_tf and iso != "USA" else ""),
                   tax_src, OECD_PAGE_URL, str(tax_year.get(iso))),
            st_row("U.S. 10Y government yield", pct(y_us, 3) + (" (manual)" if ov_yus else ""), ylds,
                   TE_PAGE_URL, f"{ylds['as_of']} · quote {ytab.loc['USA', 'reference']}"),
            st_row(f"{cname} 10Y government yield", pct(y_f, 3) + (" (manual)" if ov_yf and iso != "USA" else ""),
                   ylds, TE_PAGE_URL, f"{ylds['as_of']} · quote {ref}"),
        ]
        st.dataframe(pd.DataFrame(srows), hide_index=True,
                     column_config={"Source": st.column_config.LinkColumn("Source")})
        st.markdown("""
- **Betas:** column *Unlevered beta* of Damodaran's U.S. *Industry Averages* sheet.
- **Spread:** *Baa2/BBB* row of the table *For large non-financial service firms*.
- **Tax:** OECD *Corporate Tax Statistics*, combined (central + sub-central) statutory CIT rate, latest year per country. The OECD web page blocks automated reading, so the app reads the **same OECD database** through its official SDMX API.
- **Yields:** Trading Economics *Government Bond 10Y* list (scraped; no API key). Countries without an OECD tax rate (e.g. Russia, Taiwan) are not offered.
""")
        for k, v in src.items():
            if v["mode"] == "backup":
                st.warning(f"{k}: live fetch failed ({v.get('error', '')}). Using snapshot saved on {v.get('saved_on')}.")

    with t_chk:
        st.markdown("These tests re-run **live** on your current inputs every time you change something.")
        for name, ok, detail in self_checks(r):
            st.markdown(f"{'✅' if ok else '❌'} **{name}**  \n<span style='color:#5B6B80'>{detail}</span>",
                        unsafe_allow_html=True)
        st.caption("Note: the equal-yields test turns the foreign-spread override off, as the assignment requires.")

    with t_dl:
        res = pd.DataFrame([
            ("Industry", industry, industry), ("Country / currency", "United States / USD", f"{cname} / {cur}"),
            ("Project tax rate", pct(project_tax, 3), pct(project_tax, 3)),
            ("Equity risk premium", pct(erp, 2), pct(erp, 2)), ("Debt share D/V", f"{debt}%", f"{debt}%"),
            ("Unlevered beta", f"{beta_u:.4f}", f"{beta_u:.4f}"), ("Levered beta", f"{r['beta_l']:.4f}", f"{r['beta_l']:.4f}"),
            ("10Y gov't yield", pct(y_us, 3), pct(y_f, 3)), ("BBB spread", pct(spread_us, 3), pct(r["spread_f"], 3)),
            ("Cost of equity", pct(r["ke_usd"], 3), pct(r["ke_f"], 3)),
            ("Cost of debt pre-tax", pct(r["kd_usd"], 3), pct(r["kd_f"], 3)),
            ("Cost of debt after tax", pct(r["kd_usd_at"], 3), pct(r["kd_f_at"], 3)),
            ("WACC", pct(r["wacc_usd"], 3), pct(r["wacc_f"], 3)),
            ("Data status", ", ".join(f"{k}:{v['mode']}" for k, v in src.items()), ""),
            ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), ""),
        ], columns=["Item", "USD", loc])
        st.dataframe(res, hide_index=True)
        d1, d2, d3 = st.columns(3)
        d1.download_button("⬇️ This result (CSV)", res.to_csv(index=False).encode(),
                           f"wacc_{industry[:20]}_{iso}.csv".replace(" ", "_"), "text/csv", width="stretch")
        d2.download_button("⬇️ All industries (CSV)", idf.to_csv(index=False).encode(),
                           f"wacc_all_industries_{iso}.csv", "text/csv", width="stretch")
        d3.download_button("⬇️ All countries (CSV)", mdf.drop(columns=["label"]).to_csv(index=False).encode(),
                           f"wacc_all_countries_{industry[:20]}.csv".replace(" ", "_"), "text/csv", width="stretch")


if __name__ == "__main__":
    main()
