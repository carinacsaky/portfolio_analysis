"""
Product C — Synthetic Flood Losses

Applies depth-damage vulnerability curves to EFAS flood depths (RP10 / RP100 /
RP500) to estimate gross losses per location, then aggregates to portfolio-level
risk metrics: loss by return period, AAL, and OEP curve.

Damage function:
  Piecewise-linear curve for European residential buildings.
  Source: Huizinga et al. (2017), "Global flood depth-damage functions",
          EUR 28050 EN, Publications Office of the EU.

  Depth (m) :  0.0   0.5   1.0   1.5   2.0   3.0   4.0   5.0   6.0
  Damage (%) :   0    10    30    45    55    70    80    85   100

  Single generic curve — no building-type split (occupancy not in schema).

AAL:
  Trapezoidal integration over the OEP curve anchored at RP10 / RP100 / RP500.
  Loss ramps linearly from 0 at annual rate 1.0 to L(RP10).
  Tail beyond RP500 held constant at L(RP500).

Outputs (output/):
  c_loss_summary.csv    — loss and loss ratio by return period, plus AAL
  c_oep_curve.png       — Occurrence Exceedance Probability curve
  c_depth_damage.png    — damage curve used (methodology reference)
  c_loss_map.png        — location-level estimated gross loss at RP100
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

PARTITION        = "bldngs_ftprnts_ww_prt_2025_Q4"
COUNTRY          = "DEU"
PERIL            = "FLOOD"
NODATA_THRESHOLD = -1e30

RASTERS = {
    "RP10":  "/home/carina/Downloads/floodMap_RP010/floodmap_EFAS_RP010_C.tif",
    "RP20":  "/home/carina/Downloads/floodMap_RP020/floodmap_EFAS_RP020_C.tif",
    "RP50":  "/home/carina/Downloads/floodMap_RP050/floodmap_EFAS_RP050_C.tif",
    "RP100": "/home/carina/Downloads/floodMap_RP100/floodmap_EFAS_RP100_C.tif",
    "RP200": "/home/carina/Downloads/floodMap_RP200/floodmap_EFAS_RP200_C.tif",
    "RP500": "/home/carina/Downloads/floodMap_RP500/floodmap_EFAS_RP500_C.tif",
}
RETURN_PERIODS = {"RP10": 10, "RP20": 20, "RP50": 50, "RP100": 100, "RP200": 200, "RP500": 500}

# Huizinga et al. (2017) European residential depth-damage curve
_CURVE_DEPTHS   = np.array([0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0])
_CURVE_FACTORS  = np.array([0.00, 0.10, 0.30, 0.45, 0.55, 0.70, 0.80, 0.85, 1.00])


def damage_factor(depth_m: np.ndarray) -> np.ndarray:
    return np.interp(depth_m, _CURVE_DEPTHS, _CURVE_FACTORS)


def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


# ── Section 1: load ───────────────────────────────────────────────────────────

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


# ── Section 2: sample rasters ────────────────────────────────────────────────

def section2_sample(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 2: Raster Sampling ──────────────────────────────────")
    print("  Reprojecting WGS84 → EPSG:3035 … ", end="", flush=True)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    x3035, y3035 = transformer.transform(df["lng"].values, df["lat"].values)
    coords = list(zip(x3035, y3035))
    print("done")

    for rp_label, raster_path in RASTERS.items():
        print(f"  Sampling {rp_label} ({Path(raster_path).name}) … ", end="", flush=True)
        with rasterio.open(raster_path) as src:
            vals = np.array([v[0] for v in src.sample(coords, masked=False)])
        flooded = (vals > 0) & (vals > NODATA_THRESHOLD)
        df[f"depth_{rp_label}"] = np.where(flooded, vals, 0.0)
        print(f"done  ({flooded.sum():,} flooded, {flooded.mean()*100:.1f}%)")

    return df


# ── Section 3: apply damage curves → gross loss per location ──────────────────

def section3_losses(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 3: Depth-Damage → Gross Loss per Location ───────────")

    for rp_label in RASTERS:
        depth = df[f"depth_{rp_label}"].values
        dmg   = damage_factor(depth)
        loss  = df["tsi"].values * dmg
        df[f"dmg_factor_{rp_label}"] = dmg
        df[f"loss_{rp_label}"]       = loss

        flooded = depth > 0
        if flooded.any():
            print(f"  {rp_label}:  gross loss {loss.sum()/1e9:.2f} B EUR  "
                  f"| mean damage factor on flooded locs: {dmg[flooded].mean()*100:.1f}%  "
                  f"| max: {dmg[flooded].max()*100:.1f}%")

    return df


# ── Section 4: portfolio metrics and AAL ─────────────────────────────────────

def section4_metrics(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 4: Portfolio Metrics and AAL ────────────────────────")

    tsi_total = df["tsi"].sum()

    rows = []
    for rp_label, rp_val in RETURN_PERIODS.items():
        loss = df[f"loss_{rp_label}"].sum()
        rows.append(dict(
            return_period=rp_label,
            rp_years=rp_val,
            annual_rate=1 / rp_val,
            gross_loss_bn=loss / 1e9,
            loss_ratio_pct=loss / tsi_total * 100,
        ))

    summary = pd.DataFrame(rows)
    L = summary["gross_loss_bn"].values * 1e9
    r = summary["annual_rate"].values

    # Trapezoidal integration over OEP curve
    # First segment: ramp from loss=0 at rate=1.0 down to first RP
    aal = 0.5 * L[0] * (1.0 - r[0])
    # Middle segments
    for i in range(len(L) - 1):
        aal += 0.5 * (L[i] + L[i + 1]) * (r[i] - r[i + 1])
    # Tail beyond last RP: held constant at L[-1]
    aal += L[-1] * r[-1]

    print(f"\n  Gross TSI          : {tsi_total/1e9:.1f} B EUR")
    for _, row in summary.iterrows():
        print(f"  {row.return_period} gross loss : {row.gross_loss_bn:6.2f} B EUR  "
              f"({row.loss_ratio_pct:.3f}% of TSI)")
    print(f"\n  AAL (trapezoidal)  : {aal/1e9:.4f} B EUR  ({aal/tsi_total*100:.5f}% of TSI)")
    l_rp100 = summary.loc[summary["return_period"] == "RP100", "gross_loss_bn"].iloc[0] * 1e9
    print(f"  AAL / RP100 ratio  : {aal/l_rp100*100:.1f}%  "
          f"(>20% suggests heavy left tail; <5% suggests sparse RP10 losses)")

    aal_row = pd.DataFrame([dict(
        return_period="AAL", rp_years=np.nan, annual_rate=np.nan,
        gross_loss_bn=aal / 1e9, loss_ratio_pct=aal / tsi_total * 100,
    )])
    summary = pd.concat([summary, aal_row], ignore_index=True)
    summary.to_csv(OUTPUT / "c_loss_summary.csv", index=False)
    print("\n  → c_loss_summary.csv")

    return summary


# ── Section 5: plots ──────────────────────────────────────────────────────────

def section5_plots(df: pd.DataFrame, summary: pd.DataFrame):
    print("\n── Section 5: Plots ────────────────────────────────────────────")

    oep = summary[summary["return_period"] != "AAL"].copy()
    aal_bn = summary.loc[summary["return_period"] == "AAL", "gross_loss_bn"].iloc[0]

    # 5a: OEP curve
    fig, ax = plt.subplots(figsize=(9, 5))
    rp_years = oep["rp_years"].values
    losses   = oep["gross_loss_bn"].values

    ax.fill_between([1, *rp_years, 2000], [0, *losses, losses[-1]],
                    alpha=0.08, color="#1a6faf", step=None)
    ax.plot(rp_years, losses, "o-", color="#1a6faf", lw=2, ms=8, zorder=3)
    for rp, loss, lr in zip(rp_years, losses, oep["loss_ratio_pct"].values):
        ax.annotate(f"{loss:.2f} B EUR\n({lr:.2f}% LR)",
                    xy=(rp, loss), xytext=(12, 4),
                    textcoords="offset points", fontsize=8.5)

    ax.axhline(aal_bn, color="#ED7D31", lw=1.5, linestyle="--",
               label=f"AAL = {aal_bn:.4f} B EUR")
    ax.set_xscale("log")
    ax.set_xticks(rp_years)
    ax.set_xticklabels([str(int(r)) for r in rp_years])
    ax.set_xlabel("Return period (years)")
    ax.set_ylabel("Gross loss (B EUR)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Occurrence Exceedance Probability curve")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUTPUT / "c_oep_curve.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → c_oep_curve.png")

    # 5b: depth-damage curve illustration
    depth_x = np.linspace(0, 6.5, 300)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(depth_x, damage_factor(depth_x) * 100, color="#1a6faf", lw=2)
    ax.scatter(_CURVE_DEPTHS, _CURVE_FACTORS * 100,
               color="#C00000", zorder=5, s=50, label="Control points")
    ax.set_xlabel("Flood depth (m)")
    ax.set_ylabel("Damage factor (% of insured value)")
    ax.set_title("Depth-damage curve — Huizinga et al. (2017), European residential")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.3)
    plt.tight_layout()
    fig.savefig(OUTPUT / "c_depth_damage.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → c_depth_damage.png")

    # 5c: loss map at RP100
    loss_col = "loss_RP100"
    has_loss = df[loss_col] > 0
    cap = df.loc[has_loss, loss_col].quantile(0.995)

    RP100_loss_bn = oep.loc[oep["return_period"] == "RP100", "gross_loss_bn"].iloc[0]

    fig, ax = plt.subplots(figsize=(9, 10))
    ax.scatter(df.loc[~has_loss, "lng"], df.loc[~has_loss, "lat"],
               s=0.1, color="#d0d0d0", alpha=0.25, rasterized=True, label="No loss")
    sc = ax.scatter(df.loc[has_loss, "lng"], df.loc[has_loss, "lat"],
                    c=df.loc[has_loss, loss_col].clip(upper=cap),
                    cmap="YlOrRd", s=0.5, alpha=0.75, rasterized=True,
                    vmin=0, vmax=cap, linewidths=0)
    plt.colorbar(sc, ax=ax, label="Estimated gross loss (EUR, capped at 99.5th pct)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Estimated gross loss at RP100\n"
                 f"(total {RP100_loss_bn:.2f} B EUR)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(markerscale=8, fontsize=9, loc="lower right")
    plt.tight_layout()
    fig.savefig(OUTPUT / "c_loss_map.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → c_loss_map.png")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng     = make_engine()
    df      = section1_load(eng)
    df      = section2_sample(df)
    df      = section3_losses(df)
    summary = section4_metrics(df)
    section5_plots(df, summary)

    print("\nProduct C (synthetic losses) complete. All outputs in output/")
