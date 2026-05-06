"""
U4 — Scale-Invariance Diagnostics

Answers: at what spatial scale does concentration risk actually live?

Three complementary lenses:
  1. Multi-scale HHI / Gini  — H3 resolutions 3–8 (~111 km → ~0.9 km)
  2. Ripley's L function      — sampled TSI-weighted point cloud vs CSR null
  3. Correlation dimension    — power-law exponent of pair count vs scale

Three possible curve shapes:
  Flat      → scale-invariant (fractal-like); events at any scale equally damaging
  Rising at small scale → local clustering; neighbourhood / city-block risk
  Rising at large scale → regional concentration; basin / country-level dominance

Dataset: bldngs_ftprnts_ww_prt_2025_Q4  |  country=DEU  |  peril=FLOOD

Outputs (output/):
  u4_concentration_curve.png  — HHI, Gini, eff-N vs log(scale) — main deliverable
  u4_ripley_L.png             — L(r) − r vs radius
  u4_correlation_dim.png      — log C(r) vs log r with power-law fit
  u4_summary.csv              — per-resolution concentration metrics table
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
from scipy.spatial.distance import cdist
from scipy import stats
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
rng = np.random.default_rng(42)

BASE_DIR  = Path(__file__).parent
OUTPUT    = BASE_DIR / "output"
OUTPUT.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")
_DB = (
    f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)
_CONN = dict(keepalives=1, keepalives_idle=5, keepalives_interval=2, keepalives_count=5)

PARTITION   = "bldngs_ftprnts_ww_prt_2025_Q4"
COUNTRY     = "DEU"
PERIL       = "FLOOD"
RESOLUTIONS = [3, 4, 5, 6, 7, 8]
N_SAMPLE    = 6_000       # points for Ripley / correlation-dim (RAM: ~0.3 GB)
AREA_KM2    = 357_114.0   # Germany land area

# H3 average cell area and characteristic scale per resolution
H3_AREA = {3: 12_392.26, 4: 1_770.32, 5: 252.90, 6: 36.13, 7: 5.16, 8: 0.74}
H3_SCALE = {r: np.sqrt(a) for r, a in H3_AREA.items()}  # km


# ── helpers ───────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)

def hhi(s: pd.Series) -> float:
    sh = s / s.sum()
    return float((sh ** 2).sum())

def gini(s: pd.Series) -> float:
    v = np.sort(s.values)
    n = len(v)
    idx = np.arange(1, n + 1)
    return float((2 * (idx * v).sum() / (n * v.sum())) - (n + 1) / n)


# ── Section 1: load data ──────────────────────────────────────────────────────

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


# ── Section 2: multi-scale HHI/Gini via H3 ───────────────────────────────────

def section2_multiscale(df: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 2: Multi-Scale Concentration (H3 res {RESOLUTIONS[0]}–{RESOLUTIONS[-1]}) ─")

    # Compute finest resolution first; derive coarser via cell_to_parent
    finest = RESOLUTIONS[-1]
    print(f"  H3 res {finest} (latlng → cell) … ", end="", flush=True)
    df[f"h3_{finest}"] = [
        h3.latlng_to_cell(lat, lng, finest)
        for lat, lng in zip(df["lat"].values, df["lng"].values)
    ]
    print("done")

    for res in reversed(RESOLUTIONS[:-1]):
        print(f"  H3 res {res} (cell_to_parent) … ", end="", flush=True)
        df[f"h3_{res}"] = [h3.cell_to_parent(c, res) for c in df[f"h3_{finest}"].values]
        print("done")

    rows = []
    for res in RESOLUTIONS:
        cells = df.groupby(f"h3_{res}")["tsi"].sum()
        h = hhi(cells)
        g = gini(cells)
        n_eff = 1 / h
        top1  = cells.nlargest(1).sum() / cells.sum() * 100
        top5  = cells.nlargest(5).sum() / cells.sum() * 100
        rows.append(dict(
            resolution=res,
            scale_km=H3_SCALE[res],
            n_cells=len(cells),
            hhi=h,
            gini=g,
            eff_n=n_eff,
            top1_pct=top1,
            top5_pct=top5,
        ))
        print(f"  Res {res} ({H3_SCALE[res]:>6.1f} km) — "
              f"{len(cells):>7,} cells  HHI={h:.5f}  Gini={g:.3f}  "
              f"eff_n={n_eff:.0f}  top1={top1:.2f}%")

    summary = pd.DataFrame(rows)
    summary.to_csv(OUTPUT / "u4_summary.csv", index=False)
    print("  → u4_summary.csv")
    return summary


# ── Section 3: Ripley's L function ────────────────────────────────────────────

def section3_ripley(df: pd.DataFrame):
    print(f"\n── Section 3: Ripley's L Function (n={N_SAMPLE:,} sample) ──────────")

    # TSI-weighted sample
    weights = df["tsi"].values.astype(float)
    weights /= weights.sum()
    idx = rng.choice(len(df), size=N_SAMPLE, replace=False, p=weights)
    pts = df.iloc[idx]

    # Project to km (flat-earth approximation around Germany's centroid)
    lat0 = pts["lat"].mean()
    km_lat = 111.32
    km_lng = 111.32 * np.cos(np.radians(lat0))
    xy = np.column_stack([pts["lng"].values * km_lng, pts["lat"].values * km_lat])

    print(f"  Computing {N_SAMPLE}×{N_SAMPLE} distance matrix … ", end="", flush=True)
    D = cdist(xy, xy)
    triu = D[np.triu_indices(N_SAMPLE, k=1)]
    print("done")

    radii = np.geomspace(0.5, 250, 70)
    K_vals, C_vals = [], []
    for r in radii:
        count = int((triu <= r).sum())
        K = AREA_KM2 / (N_SAMPLE * (N_SAMPLE - 1)) * 2 * count
        K_vals.append(K)
        C_vals.append(count / len(triu))

    K_arr = np.array(K_vals)
    L_vals = np.sqrt(K_arr / np.pi) - radii

    idx_peak = int(np.argmax(L_vals))
    print(f"  Peak L(r) at r ≈ {radii[idx_peak]:.1f} km  "
          f"→ characteristic clustering scale")

    return radii, L_vals, np.array(C_vals)


# ── Section 4: correlation dimension ─────────────────────────────────────────

def section4_corr_dim(radii: np.ndarray, C_vals: np.ndarray):
    print("\n── Section 4: Correlation Dimension ───────────────────────────")

    log_r = np.log(radii)
    log_C = np.log(C_vals + 1e-12)

    # Fit in the range where C is between 2% and 80% of its max
    valid = (C_vals > 0.02 * C_vals.max()) & (C_vals < 0.80 * C_vals.max())
    if valid.sum() < 5:
        print("  Insufficient range for fit — skipping")
        return None, None

    slope, intercept, r_val, _, _ = stats.linregress(log_r[valid], log_C[valid])
    print(f"  Correlation dimension D = {slope:.3f}  "
          f"(R²={r_val**2:.3f}, fit range {np.exp(log_r[valid].min()):.1f}–"
          f"{np.exp(log_r[valid].max()):.1f} km)")
    print(f"  Interpretation: D≈2 = random 2-D scatter; D<2 = clustered")

    return slope, valid


# ── Section 5: plots ──────────────────────────────────────────────────────────

def section5_plots(summary: pd.DataFrame, radii, L_vals, C_vals, corr_dim, fit_mask):
    print("\n── Section 5: Plots ────────────────────────────────────────────")

    # ── 5a: concentration curve ────────────────────────────────────────────────
    scales = summary["scale_km"].values
    fig, axes = plt.subplots(3, 1, figsize=(9, 10), sharex=True)

    ax = axes[0]
    ax.plot(scales, summary["hhi"].values, "o-", color="#4472C4", lw=2, ms=7)
    ax.set_ylabel("HHI")
    ax.set_title(f"{COUNTRY} — {PERIL}: Concentration vs Spatial Scale")
    ax.grid(axis="y", alpha=0.3)

    ax = axes[1]
    ax.plot(scales, summary["gini"].values, "s-", color="#ED7D31", lw=2, ms=7)
    ax.set_ylabel("Gini")
    ax.grid(axis="y", alpha=0.3)

    ax = axes[2]
    ax.plot(scales, summary["eff_n"].values, "^-", color="#70AD47", lw=2, ms=7)
    ax.set_ylabel("Effective N (1/HHI)")
    ax.set_xlabel("H3 cell characteristic scale (km)")
    ax.grid(axis="y", alpha=0.3)

    for ax in axes:
        ax.set_xscale("log")
        ax.set_xticks(scales)
        ax.set_xticklabels([f"{s:.0f}" for s in scales])

    # Annotate resolution labels on bottom panel
    for _, row in summary.iterrows():
        axes[2].annotate(
            f"res{int(row.resolution)}\n({int(row.n_cells):,})",
            (row.scale_km, row.eff_n),
            textcoords="offset points", xytext=(0, 8),
            ha="center", fontsize=7, color="#555555"
        )

    plt.tight_layout()
    fig.savefig(OUTPUT / "u4_concentration_curve.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u4_concentration_curve.png")

    # ── 5b: Ripley's L ────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(radii, L_vals, color="#4472C4", lw=2, label="L(r) − r")
    ax.axhline(0, color="black", lw=0.8, linestyle="--", label="CSR null (L=0)")
    ax.fill_between(radii, L_vals, 0,
                    where=L_vals > 0, alpha=0.15, color="#4472C4", label="Clustered")
    ax.fill_between(radii, L_vals, 0,
                    where=L_vals < 0, alpha=0.15, color="#C00000", label="Dispersed")

    peak_idx = int(np.argmax(L_vals))
    ax.axvline(radii[peak_idx], color="#ED7D31", lw=1.2, linestyle=":",
               label=f"Peak ≈ {radii[peak_idx]:.1f} km")

    ax.set_xscale("log")
    ax.set_xlabel("Radius r (km)")
    ax.set_ylabel("L(r) − r  (km)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Ripley's L function  (n = {N_SAMPLE:,} sample)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    plt.tight_layout()
    fig.savefig(OUTPUT / "u4_ripley_L.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u4_ripley_L.png")

    # ── 5c: correlation dimension ─────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(7, 5))
    log_r = np.log(radii)
    log_C = np.log(C_vals + 1e-12)
    ax.scatter(log_r, log_C, s=20, color="#4472C4", alpha=0.7, label="C(r)")

    if corr_dim is not None and fit_mask is not None:
        slope = corr_dim
        intercept = np.mean(log_C[fit_mask] - slope * log_r[fit_mask])
        fit_line = slope * log_r + intercept
        ax.plot(log_r[fit_mask], fit_line[fit_mask], color="#C00000", lw=2,
                label=f"Fit  D = {slope:.2f}")
        ax.axvspan(log_r[fit_mask].min(), log_r[fit_mask].max(),
                   alpha=0.08, color="#C00000", label="Fit range")

    ax.set_xlabel("log r  (log km)")
    ax.set_ylabel("log C(r)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Correlation Dimension")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    plt.tight_layout()
    fig.savefig(OUTPUT / "u4_correlation_dim.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u4_correlation_dim.png")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng = make_engine()

    df      = section1_load(eng)
    summary = section2_multiscale(df)
    radii, L_vals, C_vals = section3_ripley(df)
    corr_dim, fit_mask    = section4_corr_dim(radii, C_vals)
    section5_plots(summary, radii, L_vals, C_vals, corr_dim, fit_mask)

    print("\nU4 complete. All outputs in output/")
