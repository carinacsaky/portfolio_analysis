"""
Product B — Flood Hazard Overlay

Overlays EFAS flood hazard rasters onto portfolio locations to produce
hazard-weighted TSI — how much of the book sits in flooded zones at each
return period.

Method:
  Reproject location coordinates (WGS84) to EPSG:3035 (raster CRS), sample
  the raster at each point, classify as flooded (value > 0) or not.

Raster:
  Source : EFAS (European Flood Awareness System / Copernicus)
  CRS    : EPSG:3035 (ETRS89 / LAEA Europe)
  Res    : 100 m
  NoData : ~-3.4e38

Add more return periods by extending RASTERS dict at the top.

Outputs (output/):
  b_flood_summary.csv        — TSI in / out of floodplain per return period
  b_flood_exposure.png       — bar chart: % TSI flooded per return period
  b_flood_map.png            — map of flooded vs not-flooded locations (DEU)
"""

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import rasterio
from pyproj import Transformer
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
OUTPUT   = BASE_DIR / "output"
OUTPUT.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")
_DB = (
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)
_CONN = dict(keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=5)

PARTITION = "bldngs_ftprnts_ww_prt_2025_Q4"
COUNTRY   = "DEU"
PERIL     = "FLOOD"
NODATA_THRESHOLD = -1e30   # anything below this is nodata

# Add RP100 and RP500 paths here when downloaded
RASTERS = {
    "RP10":  "/home/carina/Downloads/floodMap_RP010/floodmap_EFAS_RP010_C.tif",
    "RP100": "/home/carina/Downloads/floodMap_RP100/floodmap_EFAS_RP100_C.tif",
    "RP500": "/home/carina/Downloads/floodMap_RP500/floodmap_EFAS_RP500_C.tif",
}


# ── DB ────────────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


# ── Section 1: load locations ─────────────────────────────────────────────────

def section1_load(eng) -> pd.DataFrame:
    print(f"\n── Section 1: Load {COUNTRY} / {PERIL} ────────────────────────────")
    sql = text(f"""
        SELECT lat, lng, insured_value_gross AS tsi
        FROM "{PARTITION}"
        WHERE country = :c AND covered_peril = :p
    """)
    print("  Querying … ", end="", flush=True)
    df = pd.read_sql(sql, eng, params={"c": COUNTRY, "p": PERIL})
    print(f"{len(df):,} rows  |  TSI {df['tsi'].sum()/1e9:.1f} B EUR")
    return df


# ── Section 2: reproject and sample rasters ───────────────────────────────────

def section2_sample(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 2: Raster Sampling ──────────────────────────────────")

    # Reproject WGS84 → EPSG:3035 once (shared across all rasters)
    print("  Reprojecting coordinates WGS84 → EPSG:3035 … ", end="", flush=True)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    x3035, y3035 = transformer.transform(df["lng"].values, df["lat"].values)
    print("done")

    coords = list(zip(x3035, y3035))

    for rp_label, raster_path in RASTERS.items():
        print(f"  Sampling {rp_label} ({Path(raster_path).name}) … ", end="", flush=True)

        with rasterio.open(raster_path) as src:
            vals = np.array([v[0] for v in src.sample(coords, masked=False)])

        # Flooded = valid value > 0; nodata or ≤ 0 = not flooded
        flooded = (vals > 0) & (vals > NODATA_THRESHOLD)
        df[f"depth_{rp_label}"] = np.where(flooded, vals, 0.0)
        df[f"flooded_{rp_label}"] = flooded
        print(f"done  ({flooded.sum():,} flooded locations, "
              f"{flooded.mean()*100:.1f}% of total)")

    return df


# ── Section 3: TSI exposure by hazard band ────────────────────────────────────

def section3_exposure(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 3: TSI Exposure by Hazard Band ──────────────────────")

    tsi_total = df["tsi"].sum()
    rows = []

    for rp_label in RASTERS:
        flooded_mask = df[f"flooded_{rp_label}"]
        tsi_flooded  = df.loc[flooded_mask, "tsi"].sum()
        n_flooded    = flooded_mask.sum()

        # Depth bands for flooded locations
        depth = df.loc[flooded_mask, f"depth_{rp_label}"]
        bands = pd.cut(depth, bins=[0, 0.5, 1, 2, 5, np.inf],
                       labels=["0–0.5m", "0.5–1m", "1–2m", "2–5m", ">5m"])
        band_tsi = df.loc[flooded_mask].groupby(bands, observed=True)["tsi"].sum()

        print(f"\n  {rp_label}:")
        print(f"    Flooded locations : {n_flooded:>10,}  ({n_flooded/len(df)*100:.1f}%)")
        print(f"    Flooded TSI       : {tsi_flooded/1e9:>10.1f} B EUR  "
              f"({tsi_flooded/tsi_total*100:.1f}% of total)")
        print(f"    TSI by depth band:")
        for band, val in band_tsi.items():
            print(f"      {band:<8} : {val/1e9:>7.1f} B EUR  ({val/tsi_flooded*100:.1f}% of flooded)")

        rows.append(dict(
            return_period=rp_label,
            n_locations=len(df),
            n_flooded=int(n_flooded),
            pct_flooded=n_flooded / len(df) * 100,
            tsi_total_bn=tsi_total / 1e9,
            tsi_flooded_bn=tsi_flooded / 1e9,
            pct_tsi_flooded=tsi_flooded / tsi_total * 100,
        ))

    summary = pd.DataFrame(rows)
    summary.to_csv(OUTPUT / "b_flood_summary.csv", index=False)
    print("\n  → b_flood_summary.csv")
    return summary


# ── Section 4: plots ──────────────────────────────────────────────────────────

def section4_plots(df: pd.DataFrame, summary: pd.DataFrame):
    print("\n── Section 4: Plots ────────────────────────────────────────────")

    # 4a: % TSI flooded per return period
    fig, ax = plt.subplots(figsize=(8, 5))
    rps   = summary["return_period"].values
    pcts  = summary["pct_tsi_flooded"].values
    bars  = ax.bar(rps, pcts, color="#4472C4", width=0.4, alpha=0.85)
    for bar, pct, tsi in zip(bars, pcts, summary["tsi_flooded_bn"].values):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.2,
                f"{pct:.1f}%\n({tsi:.0f} B EUR)",
                ha="center", va="bottom", fontsize=10)
    ax.set_ylabel("% of total gross TSI in floodplain")
    ax.set_xlabel("Return period")
    ax.set_title(f"{COUNTRY} — {PERIL}: TSI in floodplain by return period")
    ax.set_ylim(0, max(pcts) * 1.3)
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUTPUT / "b_flood_exposure.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → b_flood_exposure.png")

    # 4b: map of flooded vs not-flooded (first return period)
    first_rp = list(RASTERS.keys())[0]
    flooded  = df[f"flooded_{first_rp}"]

    fig, ax = plt.subplots(figsize=(9, 10))
    ax.scatter(df.loc[~flooded, "lng"], df.loc[~flooded, "lat"],
               s=0.1, color="#cccccc", alpha=0.3, label="Not flooded", rasterized=True)
    ax.scatter(df.loc[flooded, "lng"], df.loc[flooded, "lat"],
               s=0.3, color="#1a6faf", alpha=0.6, label=f"Flooded ({first_rp})", rasterized=True)
    ax.set_title(f"{COUNTRY} — Portfolio locations in {first_rp} floodplain\n"
                 f"({flooded.sum():,} of {len(df):,} locations, "
                 f"{df.loc[flooded,'tsi'].sum()/1e9:.0f} B EUR)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(markerscale=10, fontsize=9)
    plt.tight_layout()
    fig.savefig(OUTPUT / "b_flood_map.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → b_flood_map.png")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng     = make_engine()
    df      = section1_load(eng)
    df      = section2_sample(df)
    summary = section3_exposure(df)
    section4_plots(df, summary)

    print("\nProduct B (flood hazard) complete. All outputs in output/")
