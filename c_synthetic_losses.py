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
  c_top_cells.csv       — top 20 H3 res-7 cells by RP100 gross loss
  c_oep_curve.png       — Occurrence Exceedance Probability curve (gross and net)
  c_depth_damage.png    — damage curve used (methodology reference)
  c_loss_map.png        — location-level estimated gross loss at RP100
"""

import os
import warnings
from pathlib import Path

import h3
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
        SELECT lat, lng, insured_value_gross AS tsi, insured_value_net AS tsi_net
        FROM "{PARTITION}"
        WHERE country = :c AND covered_peril = :p
    """)
    print("  Querying … ", end="", flush=True)
    df = pd.read_sql(sql, eng, params={"c": COUNTRY, "p": PERIL})
    print(f"{len(df):,} rows  |  Gross TSI {df['tsi'].sum()/1e9:.1f} B EUR  "
          f"| Net TSI {df['tsi_net'].sum()/1e9:.1f} B EUR")
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
        df[f"loss_{rp_label}"]      = loss
        df[f"loss_net_{rp_label}"]  = df["tsi_net"].values * dmg

        flooded = depth > 0
        if flooded.any():
            print(f"  {rp_label}:  gross {loss.sum()/1e9:.2f} B EUR  "
                  f"| net {df[f'loss_net_{rp_label}'].sum()/1e9:.2f} B EUR  "
                  f"| mean dmg factor: {dmg[flooded].mean()*100:.1f}%")

    return df


# ── Section 4: portfolio metrics and AAL ─────────────────────────────────────

def section4_metrics(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 4: Portfolio Metrics and AAL ────────────────────────")

    tsi_gross = df["tsi"].sum()
    tsi_net   = df["tsi_net"].sum()

    rows = []
    for rp_label, rp_val in RETURN_PERIODS.items():
        gross = df[f"loss_{rp_label}"].sum()
        net   = df[f"loss_net_{rp_label}"].sum()
        rows.append(dict(
            return_period=rp_label,
            rp_years=rp_val,
            annual_rate=1 / rp_val,
            gross_loss_bn=gross / 1e9,
            gross_lr_pct=gross / tsi_gross * 100,
            net_loss_bn=net / 1e9,
            net_lr_pct=net / tsi_net * 100,
        ))

    summary = pd.DataFrame(rows)
    r = summary["annual_rate"].values

    def trapz_aal(losses):
        aal = 0.5 * losses[0] * (1.0 - r[0])
        for i in range(len(losses) - 1):
            aal += 0.5 * (losses[i] + losses[i + 1]) * (r[i] - r[i + 1])
        aal += losses[-1] * r[-1]
        return aal

    Lg = summary["gross_loss_bn"].values * 1e9
    Ln = summary["net_loss_bn"].values * 1e9
    aal_gross = trapz_aal(Lg)
    aal_net   = trapz_aal(Ln)

    print(f"\n  Gross TSI : {tsi_gross/1e9:.1f} B EUR  |  Net TSI : {tsi_net/1e9:.1f} B EUR  "
          f"(net/gross ratio: {tsi_net/tsi_gross*100:.1f}%)")
    print(f"\n  {'RP':<8} {'Gross loss':>12}  {'Gross LR':>9}  {'Net loss':>10}  {'Net LR':>8}  {'Ceded':>10}")
    for _, row in summary.iterrows():
        ceded = row.gross_loss_bn - row.net_loss_bn
        print(f"  {row.return_period:<8} {row.gross_loss_bn:>10.2f} B  "
              f"{row.gross_lr_pct:>8.3f}%  {row.net_loss_bn:>8.2f} B  "
              f"{row.net_lr_pct:>7.3f}%  {ceded:>8.2f} B")
    print(f"\n  Gross AAL : {aal_gross/1e9:.4f} B EUR  ({aal_gross/tsi_gross*100:.5f}% of gross TSI)")
    print(f"  Net AAL   : {aal_net/1e9:.4f} B EUR  ({aal_net/tsi_net*100:.5f}% of net TSI)")
    print(f"  Ceded AAL : {(aal_gross-aal_net)/1e9:.4f} B EUR  "
          f"(reinsurance covers {(aal_gross-aal_net)/aal_gross*100:.1f}% of gross AAL)")

    aal_row = pd.DataFrame([dict(
        return_period="AAL", rp_years=np.nan, annual_rate=np.nan,
        gross_loss_bn=aal_gross / 1e9, gross_lr_pct=aal_gross / tsi_gross * 100,
        net_loss_bn=aal_net / 1e9, net_lr_pct=aal_net / tsi_net * 100,
    )])
    summary = pd.concat([summary, aal_row], ignore_index=True)
    summary.to_csv(OUTPUT / "c_loss_summary.csv", index=False)
    print("\n  → c_loss_summary.csv")

    # Top 20 H3 cells by RP100 gross loss (res 7 ≈ 2.3 km, matches U1/U4)
    print(f"\n── Section 4b: Top 20 H3 Cells (res 7) by RP100 Gross Loss ────")
    df["h3_7"] = [h3.latlng_to_cell(la, lo, 7)
                  for la, lo in zip(df["lat"].values, df["lng"].values)]
    cell_loss = (df[df["loss_RP100"] > 0]
                 .groupby("h3_7")
                 .agg(
                     n_locations=("tsi", "count"),
                     tsi_M=("tsi", lambda x: x.sum() / 1e6),
                     loss_M=("loss_RP100", lambda x: x.sum() / 1e6),
                     loss_net_M=("loss_net_RP100", lambda x: x.sum() / 1e6),
                     mean_depth=("depth_RP100", "mean"),
                 )
                 .reset_index())
    cell_loss["lat"] = cell_loss["h3_7"].map(lambda c: h3.cell_to_latlng(c)[0])
    cell_loss["lng"] = cell_loss["h3_7"].map(lambda c: h3.cell_to_latlng(c)[1])
    cell_loss["lr_pct"] = cell_loss["loss_M"] / cell_loss["tsi_M"] * 100
    top20_cells = cell_loss.nlargest(20, "loss_M").reset_index(drop=True)
    top20_cells.index = range(1, 21)

    print(f"\n  {'#':>3}  {'Lat':>8}  {'Lng':>8}  {'Locs':>6}  {'TSI (M)':>8}  "
          f"{'Depth (m)':>9}  {'LR%':>6}  {'Gross loss (M)':>14}  {'Net loss (M)':>12}")
    for i, r in top20_cells.iterrows():
        print(f"  {i:>3}  {r.lat:>8.4f}  {r.lng:>8.4f}  {int(r.n_locations):>6}  "
              f"{r.tsi_M:>8.1f}  {r.mean_depth:>9.2f}  {r.lr_pct:>6.1f}  "
              f"{r.loss_M:>14.1f}  {r.loss_net_M:>12.1f}")

    top20_cells[["lat", "lng", "n_locations", "tsi_M", "mean_depth",
                 "lr_pct", "loss_M", "loss_net_M"]].to_csv(
        OUTPUT / "c_top_cells.csv", index_label="rank")
    print("\n  → c_top_cells.csv")

    return summary


def section5_plots(df: pd.DataFrame, summary: pd.DataFrame):
    print("\n── Section 5: Plots ────────────────────────────────────────────")

    oep     = summary[summary["return_period"] != "AAL"].copy()
    aal_g   = summary.loc[summary["return_period"] == "AAL", "gross_loss_bn"].iloc[0]
    aal_n   = summary.loc[summary["return_period"] == "AAL", "net_loss_bn"].iloc[0]

    # 5a: OEP curve — gross and net
    fig, ax = plt.subplots(figsize=(10, 5))
    rp_years  = oep["rp_years"].values
    losses_g  = oep["gross_loss_bn"].values
    losses_n  = oep["net_loss_bn"].values

    # Ceded shading between gross and net
    ax.fill_between([1, *rp_years, 2000],
                    [0, *losses_n, losses_n[-1]],
                    [0, *losses_g, losses_g[-1]],
                    alpha=0.12, color="#C00000", label="Ceded (reinsurance)")
    ax.fill_between([1, *rp_years, 2000], [0, *losses_n, losses_n[-1]],
                    alpha=0.10, color="#1a6faf")

    ax.plot(rp_years, losses_g, "o-", color="#1a6faf", lw=2, ms=7, zorder=3,
            label=f"Gross OEP (AAL = {aal_g:.2f} B EUR)")
    ax.plot(rp_years, losses_n, "s--", color="#C00000", lw=2, ms=7, zorder=3,
            label=f"Net OEP   (AAL = {aal_n:.2f} B EUR)")

    for rp, lg, ln in zip(rp_years, losses_g, losses_n):
        ax.annotate(f"{lg:.1f}", xy=(rp, lg), xytext=(0, 6),
                    textcoords="offset points", ha="center", fontsize=7.5, color="#1a6faf")
        ax.annotate(f"{ln:.1f}", xy=(rp, ln), xytext=(0, -13),
                    textcoords="offset points", ha="center", fontsize=7.5, color="#C00000")

    ax.set_xscale("log")
    ax.set_xticks(rp_years)
    ax.set_xticklabels([str(int(r)) for r in rp_years])
    ax.set_xlabel("Return period (years)")
    ax.set_ylabel("Loss (B EUR)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Gross vs Net OEP curve")
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
