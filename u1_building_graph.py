"""
U1 — Building Graph (Building Stack Detection)

Detects locations that share the same physical building — multiple policies
on the same structure. A single event (fire, flood) hitting that building
affects all stacked locations simultaneously, making them more correlated
than their geographic separation from other risks suggests.

Method:
  Convert lat/lng to metres, round to a 20 m grid. A genuine building stack
  is a grid cell with 2+ rows for the SAME peril — unambiguously multiple
  policies on one building. Single buildings with multi-peril coverage
  (1 row per peril) are correctly excluded.

Concentration metric:
  HHI on building-level TSI → effective number of buildings.
  Compared against effective number of geographic zones from Product A,
  this gap quantifies how much geographic diversification is illusory.

Outputs (output/):
  u1_stack_summary.csv    — per genuine stack: policy count, TSI, centroid, peril
  u1_stack_stats.png      — distribution of stack size and TSI
  u1_top_stacks.png       — top 20 stacks by TSI
  u1_concentration.png    — effective buildings vs effective zones comparison
  u1_stack_map.html       — interactive map of stacks sized by TSI
"""

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
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

PARTITION = "bldngs_ftprnts_ww_prt_2025_Q4"
COUNTRY   = "DEU"
GRID_M    = 20        # metres — tolerance for geocoding jitter
M_PER_LAT = 111_320  # metres per degree latitude


# ── DB ────────────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


def hhi(s: pd.Series) -> float:
    sh = s / s.sum()
    return float((sh ** 2).sum())


# ── Section 1: load ───────────────────────────────────────────────────────────

def section1_load(eng) -> pd.DataFrame:
    print(f"\n── Section 1: Load {COUNTRY} (all perils) ──────────────────────")
    sql = text(f"""
        SELECT lat, lng, covered_peril, insured_value_gross AS tsi
        FROM "{PARTITION}"
        WHERE country = :c
    """)
    print("  Querying … ", end="", flush=True)
    df = pd.read_sql(sql, eng, params={"c": COUNTRY})
    print(f"{len(df):,} rows  |  TSI {df['tsi'].sum()/1e9:.1f} B EUR")
    return df


# ── Section 2: genuine stack detection ───────────────────────────────────────

def section2_stacks(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 2: Genuine Stack Detection ({GRID_M} m grid) ────────────")

    lat0      = df["lat"].mean()
    m_per_lng = M_PER_LAT * np.cos(np.radians(lat0))

    df["y_cell"] = (df["lat"] * M_PER_LAT / GRID_M).round().astype(int)
    df["x_cell"] = (df["lng"] * m_per_lng  / GRID_M).round().astype(int)
    df["cell"]   = df["x_cell"].astype(str) + "_" + df["y_cell"].astype(str)

    # Group by (cell, peril) — 2+ rows here = multiple policies, same building, same peril
    cell_peril = (
        df.groupby(["cell", "covered_peril"])
        .agg(n_policies=("tsi", "count"),
             tsi=("tsi", "sum"),
             lat=("lat", "mean"),
             lng=("lng", "mean"))
        .reset_index()
    )

    # Genuine stacks: same peril, same building, more than one policy
    genuine = cell_peril[cell_peril["n_policies"] >= 2].copy()
    genuine = genuine.sort_values("tsi", ascending=False).reset_index(drop=True)

    tsi_total  = df["tsi"].sum()
    tsi_stacked = genuine["tsi"].sum()
    n_stacked_policies = genuine["n_policies"].sum()
    n_total_rows = len(df)

    print(f"  Total rows (locations × perils) : {n_total_rows:>12,}")
    print(f"  Genuine stacks (cell × peril)   : {len(genuine):>12,}")
    print(f"  Policies in stacks              : {n_stacked_policies:>12,}  ({n_stacked_policies/n_total_rows*100:.1f}% of total)")
    print(f"  TSI in stacks                   : {tsi_stacked/1e9:>12.1f} B EUR  ({tsi_stacked/tsi_total*100:.1f}% of total)")

    print(f"\n  Stack size distribution (policies per building per peril):")
    for size, count in genuine["n_policies"].value_counts().sort_index().head(10).items():
        print(f"    {size:>3} policies : {count:>8,} stacks")

    print(f"\n  Breakdown by peril:")
    for peril, grp in genuine.groupby("covered_peril"):
        print(f"    {peril:<12}  {len(grp):>7,} stacks  |  "
              f"TSI {grp['tsi'].sum()/1e9:.1f} B EUR  |  "
              f"max stack {grp['n_policies'].max()} policies")

    print(f"\n  Top 10 genuine stacks by TSI:")
    top10 = genuine.head(10)[["covered_peril", "n_policies", "tsi", "lat", "lng"]].copy()
    top10["tsi_M_eur"] = top10["tsi"] / 1e6
    print(top10[["covered_peril", "n_policies", "tsi_M_eur", "lat", "lng"]]
          .to_string(index=False, float_format="{:.4f}".format))

    genuine.to_csv(OUTPUT / "u1_stack_summary.csv", index=False)
    print("\n  → u1_stack_summary.csv")

    return df, genuine


# ── Section 3: concentration metrics ─────────────────────────────────────────

def section3_concentration(df: pd.DataFrame, genuine: pd.DataFrame):
    print("\n── Section 3: Stack Concentration ─────────────────────────────")

    tsi_total = df["tsi"].sum()

    # HHI on stacked TSI — how concentrated are the stacks themselves?
    h = hhi(genuine["tsi"])
    eff_n = 1 / h

    # Top-N share of stacked TSI
    sorted_tsi = genuine["tsi"].sort_values(ascending=False).reset_index(drop=True)
    cumsum     = sorted_tsi.cumsum() / sorted_tsi.sum()
    top10_share  = cumsum.iloc[9]   * 100
    top100_share = cumsum.iloc[99]  * 100
    top1k_share  = cumsum.iloc[999] * 100

    print(f"  Stacked TSI HHI        : {h:.6f}  →  eff_n = {eff_n:,.0f} equivalent stacks")
    print(f"  Top 10 stacks          : {top10_share:.1f}% of all stacked TSI")
    print(f"  Top 100 stacks         : {top100_share:.1f}% of all stacked TSI")
    print(f"  Top 1,000 stacks       : {top1k_share:.1f}% of all stacked TSI")
    print(f"  Max single-address TSI : {genuine['tsi'].max()/1e6:.2f} M EUR  "
          f"({genuine['tsi'].max()/tsi_total*100:.4f}% of country total)")

    # Plot: Lorenz curve of stacked TSI + top-N share bars
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))

    # Lorenz curve
    n = len(sorted_tsi)
    lorenz_x = np.linspace(0, 1, n)
    lorenz_y = sorted_tsi.sort_values().cumsum().values / sorted_tsi.sum()
    ax1.plot(lorenz_x, lorenz_y, color="#4472C4", lw=2, label="Lorenz curve")
    ax1.plot([0, 1], [0, 1], color="grey", lw=1, linestyle="--", label="Perfect equality")
    ax1.fill_between(lorenz_x, lorenz_y, lorenz_x, alpha=0.12, color="#4472C4")
    ax1.set_xlabel("Cumulative share of stacks (ranked by TSI)")
    ax1.set_ylabel("Cumulative share of stacked TSI")
    ax1.set_title("Lorenz curve — stacked building TSI")
    ax1.legend(fontsize=9)
    ax1.grid(alpha=0.25)

    # Top-N shares
    ns     = [10, 100, 1_000, 10_000]
    shares = [cumsum.iloc[n - 1] * 100 for n in ns]
    ax2.bar([str(n) for n in ns], shares, color="#ED7D31", alpha=0.85, width=0.5)
    for i, (n, s) in enumerate(zip(ns, shares)):
        ax2.text(i, s + 0.5, f"{s:.1f}%", ha="center", va="bottom", fontsize=10)
    ax2.set_xlabel("Top-N stacks")
    ax2.set_ylabel("% of total stacked TSI")
    ax2.set_title("Top-N share of stacked TSI")
    ax2.set_ylim(0, 110)
    ax2.grid(axis="y", alpha=0.3)

    plt.suptitle(f"{COUNTRY} — Building stack concentration", fontsize=12)
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_concentration.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_concentration.png")

    return h, eff_n


# ── Section 4: plots ──────────────────────────────────────────────────────────

def section4_plots(genuine: pd.DataFrame):
    print("\n── Section 4: Plots ────────────────────────────────────────────")

    # 4a: stack size distribution and TSI distribution
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))

    size_counts = genuine["n_policies"].value_counts().sort_index()
    ax1.bar(size_counts.index.astype(str), size_counts.values,
            color="#4472C4", alpha=0.85)
    ax1.set_xlabel("Policies per building (same peril)")
    ax1.set_ylabel("Number of stacks")
    ax1.set_title("Stack size distribution")

    ax2.hist(genuine["tsi"] / 1e6, bins=60, color="#ED7D31", alpha=0.85)
    ax2.set_xlabel("Stack TSI (EUR M)")
    ax2.set_ylabel("Number of stacks")
    ax2.set_title("Stack TSI distribution")
    ax2.set_yscale("log")

    plt.suptitle(f"{COUNTRY} — Genuine building stacks ({GRID_M} m grid)", fontsize=12)
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_stack_stats.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_stack_stats.png")

    # 4b: top 20 stacks by TSI
    top20 = genuine.head(20).copy()
    top20["label"] = (top20["lat"].round(4).astype(str) + ", "
                      + top20["lng"].round(4).astype(str))

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.barh(range(len(top20)), top20["tsi"].values / 1e6,
            color="#4472C4", alpha=0.85)
    ax.set_yticks(range(len(top20)))
    ax.set_yticklabels(
        [f"{r.label}  ({r.covered_peril}, {int(r.n_policies)} policies)"
         for r in top20.itertuples()],
        fontsize=8
    )
    ax.invert_yaxis()
    ax.set_xlabel("Stack gross TSI (EUR M)")
    ax.set_title(f"{COUNTRY} — Top 20 genuine building stacks by TSI")
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_top_stacks.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_top_stacks.png")

    # 4c: interactive map (top 500 stacks by TSI)
    m = folium.Map(
        location=[genuine["lat"].mean(), genuine["lng"].mean()],
        zoom_start=6, tiles="CartoDB positron"
    )

    max_tsi   = genuine["tsi"].max()
    plot_data = genuine.head(500)

    for r in plot_data.itertuples():
        radius = max(4, (r.tsi / max_tsi) ** 0.5 * 25)
        folium.CircleMarker(
            location=[r.lat, r.lng],
            radius=float(radius),
            color="#c0392b",
            fill=True,
            fill_color="#e74c3c",
            fill_opacity=0.65,
            weight=1,
            tooltip=(
                f"<b>Building stack</b><br>"
                f"Peril: {r.covered_peril}<br>"
                f"Policies: {int(r.n_policies)}<br>"
                f"TSI: {r.tsi/1e6:.1f} M EUR"
            ),
        ).add_to(m)

    m.save(str(OUTPUT / "u1_stack_map.html"))
    print("  → u1_stack_map.html  (top 500 genuine stacks by TSI)")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng = make_engine()
    df              = section1_load(eng)
    df, genuine     = section2_stacks(df)
    section3_concentration(df, genuine)
    section4_plots(genuine)

    print(f"\nU1 building graph complete. All outputs in output/")
