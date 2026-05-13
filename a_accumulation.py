"""
Product A — Accumulation & Concentration Analysis

Table: bldngs_ftprnts_ww_prt_2025_Q4
  349M rows | 54 countries | 3 perils (FLOOD, FIRE, EARTHQUAKE) | version=manual_import

Sections:
  1. Portfolio inventory (A.1)      → a_inventory.csv
  2. Concentration metrics (A.3/7)  → a_concentration.png
  3. H3 spatial grid — DEU (A.2)    → a_h3_deu.png
  4. NUTS3 aggregation — DEU (A.2)  → a_nuts3_deu.png
  5. Cross-peril analysis (A.6)     → a_cross_peril.png
  6. Interactive choropleth (A.8)   → a_choropleth.html
"""

import os
import warnings
from pathlib import Path

import h3
import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import seaborn as sns
import folium
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

BASE_DIR  = Path(__file__).parent
OUTPUT    = BASE_DIR / "output"
OUTPUT.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")
_DB = (
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)
_CONN = dict(keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=5)

PARTITION  = "bldngs_ftprnts_ww_prt_2025_Q4"
PERILS     = ["FLOOD", "FIRE", "EARTHQUAKE"]
DEEP_DIVE  = "DEU"   # change to any 3-letter country code
NUTS_CODE  = "DE"    # matching 2-letter code for nuts3_eu_admin

# ISO3 → ISO2 for NUTS join
ISO3_TO_ISO2 = {
    "ALB": "AL", "AND": "AD", "ARM": "AM", "AUS": "AU", "AUT": "AT",
    "AZE": "AZ", "BEL": "BE", "BGR": "BG", "BIH": "BA", "BLR": "BY",
    "CHE": "CH", "CYP": "CY", "CZE": "CZ", "DEU": "DE", "DNK": "DK",
    "EST": "EE", "FIN": "FI", "FRA": "FR", "GEO": "GE", "GRC": "GR",
    "HRV": "HR", "HUN": "HU", "IDN": "ID", "IRL": "IE", "ISL": "IS",
    "ITA": "IT", "JPN": "JP", "LIE": "LI", "LTU": "LT", "LUX": "LU",
    "LVA": "LV", "MCO": "MC", "MDA": "MD", "MKD": "MK", "MLT": "MT",
    "MNE": "ME", "NLD": "NL", "NOR": "NO", "PHL": "PH", "POL": "PL",
    "PRT": "PT", "ROU": "RO", "RUS": "RU", "SMR": "SM", "SRB": "RS",
    "SVK": "SK", "SVN": "SI", "SWE": "SE", "TUR": "TR", "UKR": "UA",
    "VAT": "VA", "XKX": "XK",
}


# ── DB helpers ────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


def query(eng, sql: str, **params) -> pd.DataFrame:
    return pd.read_sql(text(sql), eng, params=params or None)


# ── Section 1: Portfolio inventory (A.1) ─────────────────────────────────────

def section1_inventory(eng) -> pd.DataFrame:
    print("\n── Section 1: Portfolio Inventory ─────────────────────────────")

    # PERCENTILE_CONT requires a full sort and causes SSL timeout at this scale.
    # Streaming aggregates only (COUNT, SUM, MIN, MAX).
    sql = f"""
        SELECT country, covered_peril,
               COUNT(*)                 AS n,
               SUM(insured_value_gross) AS tsi_gross,
               SUM(insured_value_net)   AS tsi_net,
               MIN(insured_value_gross) AS tsi_min,
               MAX(insured_value_gross) AS tsi_max
        FROM "{PARTITION}"
        GROUP BY country, covered_peril
        ORDER BY country, covered_peril
    """
    inv = pd.read_sql(sql, eng)

    inv["net_gross_ratio"] = inv["tsi_net"] / inv["tsi_gross"]

    by_country = inv.groupby("country").agg(
        n_locations=("n", "sum"),
        tsi_gross=("tsi_gross", "sum"),
        tsi_net=("tsi_net", "sum"),
        avg_net_gross=("net_gross_ratio", "mean"),
    ).reset_index()
    by_country["tsi_gross_bn"] = by_country["tsi_gross"] / 1e9
    by_country = by_country.sort_values("tsi_gross", ascending=False)

    print(f"  {inv['country'].nunique()} countries  |  "
          f"{inv['covered_peril'].nunique()} perils  |  "
          f"{inv['n'].sum():,.0f} total rows")
    print(f"  Total TSI (gross): {inv['tsi_gross'].sum()/1e12:.2f} T EUR")
    print(f"\n  Top 10 by TSI:")
    print(by_country.head(10)[["country","n_locations","tsi_gross_bn","avg_net_gross"]]
          .to_string(index=False, float_format="{:.2f}".format))

    inv.to_csv(OUTPUT / "a_inventory.csv", index=False)
    by_country.to_csv(OUTPUT / "a_inventory_by_country.csv", index=False)
    print("  → a_inventory.csv, a_inventory_by_country.csv")
    return inv, by_country


# ── Section 2: Concentration metrics (A.3 + A.7) ─────────────────────────────

def hhi(series: pd.Series) -> float:
    s = series / series.sum()
    return float((s ** 2).sum())

def gini(series: pd.Series) -> float:
    s = np.sort(series.values)
    n = len(s)
    idx = np.arange(1, n + 1)
    return float((2 * (idx * s).sum() / (n * s.sum())) - (n + 1) / n)

def theil(series: pd.Series) -> float:
    mu = series.mean()
    return float(((series / mu) * np.log(series / mu)).mean())


def section2_concentration(inv: pd.DataFrame, by_country: pd.DataFrame):
    print("\n── Section 2: Concentration Metrics ───────────────────────────")

    # Country-level HHI, Gini, Theil per peril
    rows = []
    for peril in PERILS:
        tsi = inv[inv["covered_peril"] == peril].set_index("country")["tsi_gross"]
        h = hhi(tsi)
        g = gini(tsi)
        t = theil(tsi)
        n_eff = 1 / h
        top5  = tsi.nlargest(5).sum()  / tsi.sum()
        top10 = tsi.nlargest(10).sum() / tsi.sum()
        rows.append(dict(peril=peril, hhi=h, gini=g, theil=t,
                         effective_n=n_eff, top5_share=top5, top10_share=top10))
        print(f"  {peril:<12}  HHI={h:.4f}  Gini={g:.3f}  "
              f"eff_n={n_eff:.1f}  Top5={top5*100:.1f}%  Top10={top10*100:.1f}%")

    conc = pd.DataFrame(rows)
    conc.to_csv(OUTPUT / "a_concentration.csv", index=False)

    # Plot: HHI and Gini side by side
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    x = np.arange(len(PERILS))
    ax1.bar(x, conc["hhi"], color=["#4472C4","#ED7D31","#A9D18E"], width=0.5)
    ax1.set_xticks(x); ax1.set_xticklabels(PERILS)
    ax1.set_title("HHI — country concentration"); ax1.set_ylabel("HHI (0=equal, 1=monopoly)")
    for xi, v in zip(x, conc["hhi"]):
        ax1.text(xi, v + 0.001, f"{v:.4f}", ha="center", fontsize=9)

    ax2.bar(x, conc["gini"], color=["#4472C4","#ED7D31","#A9D18E"], width=0.5)
    ax2.set_xticks(x); ax2.set_xticklabels(PERILS)
    ax2.set_title("Gini — country concentration"); ax2.set_ylabel("Gini")
    for xi, v in zip(x, conc["gini"]):
        ax2.text(xi, v + 0.002, f"{v:.3f}", ha="center", fontsize=9)

    plt.suptitle("Portfolio concentration by peril (country level)", fontsize=12)
    plt.tight_layout()
    fig.savefig(OUTPUT / "a_concentration.png", dpi=150, bbox_inches="tight")
    plt.close()

    # Top-20 countries chart
    top20 = by_country.head(20).copy()
    fig, ax = plt.subplots(figsize=(12, 5))
    bars = ax.barh(range(len(top20)), top20["tsi_gross_bn"].values,
                   color="#4472C4", alpha=0.85)
    ax.set_yticks(range(len(top20)))
    ax.set_yticklabels(top20["country"].values)
    ax.invert_yaxis()
    ax.set_xlabel("Total gross TSI (EUR bn)")
    ax.set_title("Top 20 countries by gross TSI")
    ax2 = ax.twinx()
    ax2.set_ylim(ax.get_ylim())
    ax2.set_yticks(range(len(top20)))
    ax2.set_yticklabels([f"n/g={r:.2f}" for r in top20["avg_net_gross"].values],
                        fontsize=8, color="grey")
    plt.tight_layout()
    fig.savefig(OUTPUT / "a_top_countries.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → a_concentration.png, a_top_countries.png")
    return conc


# ── Section 3: H3 spatial grid — DEU (A.2) ───────────────────────────────────

def section3_h3(eng) -> pd.DataFrame:
    print(f"\n── Section 3: H3 Spatial Grid ({DEEP_DIVE} / FLOOD) ───────────")

    sql = f"""
        SELECT lat, lng, insured_value_gross AS tsi
        FROM "{PARTITION}"
        WHERE country = :c AND covered_peril = 'FLOOD'
    """
    print(f"  Loading {DEEP_DIVE} FLOOD locations …", end=" ", flush=True)
    df = pd.read_sql(text(sql), eng, params={"c": DEEP_DIVE})
    print(f"{len(df):,} rows")

    results = {}
    for res in [5, 6, 7, 8]:
        df[f"h3_{res}"] = df.apply(
            lambda r: h3.latlng_to_cell(r.lat, r.lng, res), axis=1
        )
        cells = df.groupby(f"h3_{res}")["tsi"].sum().reset_index()
        cells.columns = ["cell", "tsi"]
        h = hhi(cells["tsi"])
        n_eff = 1 / h
        results[res] = dict(n_cells=len(cells), hhi=h, eff_n=n_eff,
                            top1_pct=cells["tsi"].max()/cells["tsi"].sum()*100)
        print(f"  Res {res}: {len(cells):>6,} cells  "
              f"HHI={h:.5f}  eff_n={n_eff:.0f}  "
              f"top-cell={results[res]['top1_pct']:.2f}%")

    # Hex map at resolution 7
    cells7 = (df.groupby("h3_7")["tsi"]
               .agg(["sum", "count"])
               .rename(columns={"sum": "tsi", "count": "n"})
               .reset_index())
    cells7["lat_c"] = cells7["h3_7"].apply(lambda c: h3.cell_to_latlng(c)[0])
    cells7["lng_c"] = cells7["h3_7"].apply(lambda c: h3.cell_to_latlng(c)[1])

    fig, ax = plt.subplots(figsize=(10, 8))
    sc = ax.scatter(cells7["lng_c"], cells7["lat_c"],
                    c=np.log1p(cells7["tsi"]),
                    s=cells7["n"] / cells7["n"].max() * 30 + 1,
                    cmap="YlOrRd", alpha=0.7, linewidths=0)
    plt.colorbar(sc, ax=ax, label="log(TSI) per H3 cell (res 7)")
    ax.set_title(f"{DEEP_DIVE} — H3 resolution 7 accumulation (FLOOD)")
    ax.set_xlabel("Longitude"); ax.set_ylabel("Latitude")
    plt.tight_layout()
    fig.savefig(OUTPUT / "a_h3_deu.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → a_h3_deu.png")
    return df, cells7


# ── Section 4: NUTS3 aggregation — DEU (A.2) ─────────────────────────────────

def section4_nuts3(eng, df_country: pd.DataFrame):
    print(f"\n── Section 4: NUTS3 Aggregation ({DEEP_DIVE}) ──────────────────")

    nuts_sql = f"""
        SELECT nuts_id, name_latn, geom
        FROM nuts3_eu_admin
        WHERE cntr_code = '{NUTS_CODE}' AND levl_code = 3
    """
    print("  Loading NUTS3 geometries …", end=" ", flush=True)
    nuts = gpd.read_postgis(nuts_sql, eng, geom_col="geom")
    nuts = nuts.set_crs("EPSG:3857")
    print(f"{len(nuts)} regions")

    # Build GeoDataFrame from lat/lng (WGS84 → 3857 to match NUTS)
    gdf = gpd.GeoDataFrame(
        df_country[["lat","lng","tsi"]],
        geometry=gpd.points_from_xy(df_country["lng"], df_country["lat"]),
        crs="EPSG:4326"
    ).to_crs("EPSG:3857")

    print("  Spatial join …", end=" ", flush=True)
    joined = gpd.sjoin(gdf, nuts[["nuts_id","name_latn","geom"]],
                       how="left", predicate="within")
    print("done")

    nuts_agg = (joined.dropna(subset=["nuts_id"])
                .groupby(["nuts_id","name_latn"])
                .agg(n=("tsi","count"), tsi=("tsi","sum"))
                .reset_index())

    nuts_h = hhi(nuts_agg["tsi"])
    print(f"  NUTS3 HHI={nuts_h:.5f}  eff_n={1/nuts_h:.0f}  "
          f"({len(nuts_agg)} NUTS3 regions matched)")
    top5_nuts = nuts_agg.nlargest(5, "tsi")[["nuts_id","name_latn","n","tsi"]]
    print(f"  Top 5 NUTS3 regions:\n{top5_nuts.to_string(index=False)}")

    # Choropleth
    nuts_map = nuts.merge(nuts_agg[["nuts_id","tsi","n"]], on="nuts_id", how="left")
    fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    nuts_map.plot(column="tsi", ax=ax, cmap="YlOrRd", legend=True,
                  legend_kwds={"label": "Gross TSI (EUR)", "shrink": 0.6},
                  missing_kwds={"color": "#eeeeee"})
    ax.set_title(f"{DEEP_DIVE} — Gross TSI per NUTS3 region (FLOOD)", fontsize=13)
    ax.axis("off")
    plt.tight_layout()
    fig.savefig(OUTPUT / "a_nuts3_deu.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → a_nuts3_deu.png")
    return nuts_agg


# ── Section 5: Cross-peril analysis (A.6) ────────────────────────────────────

def section5_cross_peril(inv: pd.DataFrame):
    print("\n── Section 5: Cross-Peril Analysis ────────────────────────────")

    # Net-to-gross ratio per country per peril
    inv["ng"] = inv["tsi_net"] / inv["tsi_gross"]

    # Check for variation in net-to-gross across perils within country
    pivot = inv.pivot_table(index="country", columns="covered_peril",
                            values="ng", aggfunc="mean")
    pivot["ng_range"] = pivot.max(axis=1) - pivot.min(axis=1)
    varied = pivot[pivot["ng_range"] > 0.01].sort_values("ng_range", ascending=False)

    if len(varied):
        print(f"  {len(varied)} countries with n/g ratio varying >1pp across perils:")
        print(varied.head(10).to_string(float_format="{:.3f}".format))
    else:
        print("  Net-to-gross ratio is consistent across perils for all countries.")

    # Plot net-to-gross by country (FLOOD)
    flood_ng = (inv[inv["covered_peril"] == "FLOOD"]
                .sort_values("tsi_gross", ascending=False)
                .head(30))
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.bar(range(len(flood_ng)), flood_ng["ng"].values, color="#4472C4", alpha=0.8)
    ax.set_xticks(range(len(flood_ng)))
    ax.set_xticklabels(flood_ng["country"].values, rotation=45, ha="right")
    ax.axhline(flood_ng["ng"].mean(), color="red", linestyle="--",
               linewidth=1, label=f"Mean {flood_ng['ng'].mean():.2f}")
    ax.set_ylabel("Net / Gross TSI")
    ax.set_title("Net-to-gross ratio by country (FLOOD, top 30 by TSI)")
    ax.legend()
    plt.tight_layout()
    fig.savefig(OUTPUT / "a_netgross_ratio.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → a_netgross_ratio.png")


# ── Section 6: Interactive choropleth (A.8) ──────────────────────────────────

def section6_interactive(eng, by_country: pd.DataFrame):
    print("\n── Section 6: Interactive Bubble Map ──────────────────────────")

    # country_boundaries table is empty; use NUTS3 centroids for EU countries
    # and hardcoded lat/lng for the non-EU countries in the portfolio.
    NON_EU = {
        "AUS": (-25.3, 133.8), "IDN": (-5.0, 120.0),
        "JPN": (36.2, 138.3),  "PHL": (12.9, 121.8),
    }

    cent_sql = """
        SELECT cntr_code,
               AVG(ST_Y(ST_Transform(ST_Centroid(geom), 4326))) AS lat,
               AVG(ST_X(ST_Transform(ST_Centroid(geom), 4326))) AS lng
        FROM nuts3_eu_admin
        WHERE levl_code = 3
        GROUP BY cntr_code
    """
    eu_cents = pd.read_sql(cent_sql, eng)
    eu_cents["iso3"] = eu_cents["cntr_code"].map(
        {v: k for k, v in ISO3_TO_ISO2.items()}
    )

    non_eu = pd.DataFrame(
        [(k, v[0], v[1]) for k, v in NON_EU.items()],
        columns=["iso3", "lat", "lng"]
    )
    centers = pd.concat([eu_cents[["iso3","lat","lng"]], non_eu], ignore_index=True)
    data = by_country.merge(centers, left_on="country", right_on="iso3", how="left")
    data = data.dropna(subset=["lat","lng"])

    # Scale circles: radius proportional to sqrt(TSI) for visual area ∝ TSI
    max_tsi = data["tsi_gross_bn"].max()
    data["radius"] = (data["tsi_gross_bn"] / max_tsi).pow(0.5) * 80

    m = folium.Map(location=[30, 20], zoom_start=2, tiles="CartoDB positron")

    for _, r in data.iterrows():
        folium.CircleMarker(
            location=[r.lat, r.lng],
            radius=float(r.radius),
            color="#c0392b",
            fill=True,
            fill_color="#e74c3c",
            fill_opacity=0.6,
            weight=1,
            tooltip=(
                f"<b>{r.country}</b><br>"
                f"TSI: {r.tsi_gross_bn:.1f} B EUR<br>"
                f"Locations: {r.n_locations:,.0f}<br>"
                f"Net/Gross: {r.avg_net_gross:.2f}"
            ),
        ).add_to(m)

    m.save(str(OUTPUT / "a_choropleth.html"))
    print("  → a_choropleth.html  (bubble map — radius ∝ √TSI)")
    


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng = make_engine()

    inv, by_country   = section1_inventory(eng)
    conc              = section2_concentration(inv, by_country)
    df_deu, cells7    = section3_h3(eng)
    nuts_agg          = section4_nuts3(eng, df_deu)
    section5_cross_peril(inv)
    section6_interactive(eng, by_country)

    print("\nProduct A complete. All outputs in output/")
