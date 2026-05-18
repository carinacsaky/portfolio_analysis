"""
U2 — Temporal Accumulation Analysis

Real portfolio tables: bldngs_ftprnts_ww_prt_2025_Q3 and bldngs_ftprnts_ww_prt_2025_Q4.
ITA (Italy) is the only country present in both quarters and is used as the deep-dive.
Q3 contains ITA only; Q4 contains 54 countries.

Italy TSI halved from 9,308 B EUR (Q3) to 4,657 B EUR (Q4) — this is likely a data
process change rather than real attrition and should be investigated.

Outputs (all in output/):
  u2_portfolio_overview.png   — portfolio size Q3 vs Q4
  u2_entry_exit.csv           — country entry/exit table
  u2_ita_decomposition.csv    — per-peril ΔTSI breakdown
  u2_ita_decomposition.png    — waterfall chart
  u2_ita_tsi_distribution.png — TSI histograms Q3 vs Q4
  u2_ita_concentration.png    — HHI Q3 vs Q4
  u2_ita_geographic.html      — interactive map: persisted / new / dropped
"""

import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats
import folium
from folium.plugins import FastMarkerCluster
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

PART_Q2     = "bldngs_ftprnts_ww_prt_2025_Q3"   # earlier quarter
PART_Q4     = "bldngs_ftprnts_ww_prt_2025_Q4"   # later quarter
LABEL_Q2    = "Q3 2025"
LABEL_Q4    = "Q4 2025"
DEEP_DIVE   = "ITA"
COLS        = "lat, lng, covered_peril, insured_value_gross, insured_value_net"
PERILS      = ["FLOOD", "FIRE", "EARTHQUAKE"]


# ── DB helpers ────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


def load_country(eng, partition: str, country: str) -> pd.DataFrame:
    sql = text(f'SELECT {COLS} FROM "{partition}" WHERE country = :c')
    return pd.read_sql(sql, eng, params={"c": country})


# ── Section 1: Portfolio overview ─────────────────────────────────────────────

def section1(eng):
    print("\n── Section 1: Portfolio Overview ──────────────────────────────")

    q2_sql = f"""
        SELECT country, covered_peril,
               COUNT(*)                  AS n,
               SUM(insured_value_gross)  AS tsi_gross,
               SUM(insured_value_net)    AS tsi_net,
               AVG(insured_value_gross)  AS avg_tsi
        FROM "{PART_Q2}"
        GROUP BY country, covered_peril
        ORDER BY country, covered_peril
    """
    q2 = pd.read_sql(q2_sql, eng)

    # Approximate Q4 row count via pg_class (avoids full scan on large table)
    q4_approx = pd.read_sql(text(
        "SELECT reltuples::bigint AS n FROM pg_class WHERE relname = :t"
    ), eng, params={"t": PART_Q4})["n"].iloc[0]
    q4_countries = pd.read_sql(
        text(f'SELECT COUNT(DISTINCT country) AS n FROM "{PART_Q4}"'), eng
    )["n"].iloc[0]

    q2_by_country = q2.groupby("country").agg(
        rows=("n", "sum"), tsi_gross=("tsi_gross", "sum")
    ).reset_index()

    print(f"  {LABEL_Q2}: {q2['n'].sum():>12,.0f} rows | "
          f"TSI {q2['tsi_gross'].sum()/1e9:.1f} B EUR | "
          f"{q2['country'].nunique()} countries")
    print(f"  {LABEL_Q4}: {q4_approx:>12,} rows (approx) | {q4_countries} countries")
    print(f"\n  {LABEL_Q2} per country:")
    for _, r in q2_by_country.iterrows():
        print(f"    {r.country:<6}  {r.rows:>12,.0f} rows  "
              f"TSI {r.tsi_gross/1e9:.1f} B EUR")

    q2.to_csv(OUTPUT / "u2_q2_summary.csv", index=False)

    # Query deep-dive country stats from Q4 for a like-for-like comparison
    dd_q4 = pd.read_sql(text(f"""
        SELECT COUNT(*) AS n, SUM(insured_value_gross) AS tsi_gross
        FROM "{PART_Q4}" WHERE country = :c
    """), eng, params={"c": DEEP_DIVE})
    dd_q2 = q2_by_country[q2_by_country["country"] == DEEP_DIVE].iloc[0]
    dd_q4_rows = dd_q4["n"].iloc[0]
    dd_q4_tsi  = dd_q4["tsi_gross"].iloc[0]

    # Chart — compare deep-dive country only (like-for-like)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    c1, c2 = "#4472C4", "#ED7D31"

    bars = ax1.bar([LABEL_Q2, LABEL_Q4],
                   [dd_q2["rows"] / 1e6, dd_q4_rows / 1e6],
                   color=[c1, c2], width=0.45)
    ax1.set_ylabel("Insured locations (millions)")
    ax1.set_title(f"{DEEP_DIVE} — Locations")
    for b, v in zip(bars, [dd_q2["rows"] / 1e6, dd_q4_rows / 1e6]):
        ax1.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.05,
                 f"{v:.1f} M", ha="center", va="bottom", fontsize=9)

    bars2 = ax2.bar([LABEL_Q2, LABEL_Q4],
                    [dd_q2["tsi_gross"] / 1e9, dd_q4_tsi / 1e9],
                    color=[c1, c2], width=0.45)
    ax2.set_ylabel("Gross TSI (B EUR)")
    ax2.set_title(f"{DEEP_DIVE} — Gross TSI")
    for b, v in zip(bars2, [dd_q2["tsi_gross"] / 1e9, dd_q4_tsi / 1e9]):
        ax2.text(b.get_x() + b.get_width() / 2, b.get_height() + 10,
                 f"{v:,.0f} B", ha="center", va="bottom", fontsize=9)

    plt.suptitle(f"{DEEP_DIVE}: {LABEL_Q2} → {LABEL_Q4}  "
                 f"(full portfolio: {q2['country'].nunique()} → {q4_countries} countries)",
                 fontsize=11)
    plt.tight_layout()
    fig.savefig(OUTPUT / "u2_portfolio_overview.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u2_portfolio_overview.png")

    return q2


# ── Section 2: Entry / exit ───────────────────────────────────────────────────

def section2(eng):
    print("\n── Section 2: Country Entry / Exit ────────────────────────────")

    q2_countries = set(pd.read_sql(
        text(f'SELECT DISTINCT country FROM "{PART_Q2}"'), eng
    )["country"].tolist())
    q4_countries = set(pd.read_sql(
        text(f'SELECT DISTINCT country FROM "{PART_Q4}"'), eng
    )["country"].tolist())

    both    = sorted(q2_countries & q4_countries)
    q2_only = sorted(q2_countries - q4_countries)
    q4_only = sorted(q4_countries - q2_countries)

    print(f"  {LABEL_Q2} countries : {len(q2_countries)}  ({sorted(q2_countries)})")
    print(f"  {LABEL_Q4} countries : {len(q4_countries)}")
    print(f"  In both quarters  : {len(both)}  → {both}  (valid for temporal analysis)")
    print(f"  {LABEL_Q2} only     : {len(q2_only)}  → {q2_only}")
    print(f"  {LABEL_Q4} only     : {len(q4_only)}")

    rows = (
        [dict(country=c, status="both quarters") for c in both]
        + [dict(country=c, status=f"{LABEL_Q2} only") for c in q2_only]
        + [dict(country=c, status=f"{LABEL_Q4} only — newly onboarded") for c in q4_only]
    )
    ee = pd.DataFrame(rows)
    ee.to_csv(OUTPUT / "u2_entry_exit.csv", index=False)
    print("  → u2_entry_exit.csv")
    return ee


# ── Section 3: Cuba deep-dive ─────────────────────────────────────────────────

def section3(eng):
    print(f"\n── Section 3: {DEEP_DIVE} Temporal Deep-Dive ─────────────────────────")

    print(f"  Loading {DEEP_DIVE} {LABEL_Q2} …", end=" ", flush=True)
    q2 = load_country(eng, PART_Q2, DEEP_DIVE)
    print(f"{len(q2):,} rows  |  TSI {q2['insured_value_gross'].sum()/1e9:.1f} B EUR")

    print(f"  Loading {DEEP_DIVE} {LABEL_Q4} …", end=" ", flush=True)
    q4 = load_country(eng, PART_Q4, DEEP_DIVE)
    print(f"{len(q4):,} rows  |  TSI {q4['insured_value_gross'].sum()/1e9:.1f} B EUR")

    # Flow classification: match on exact (lat, lng, covered_peril)
    # Coordinates are double-precision from the same source system —
    # exact equality join is appropriate.
    key = ["lat", "lng", "covered_peril"]

    q2_keys = q2[key].drop_duplicates()
    q4_keys = q4[key].drop_duplicates()

    merged = q2_keys.merge(q4_keys, on=key, how="outer", indicator=True)
    persisted = merged[merged["_merge"] == "both"][key]
    dropped   = merged[merged["_merge"] == "left_only"][key]
    new       = merged[merged["_merge"] == "right_only"][key]

    n_q2, n_q4 = len(q2_keys), len(q4_keys)
    print(f"\n  Flow (unique lat/lng/peril triplets):")
    print(f"    {LABEL_Q2} unique : {n_q2:>10,}")
    print(f"    {LABEL_Q4} unique : {n_q4:>10,}")
    print(f"    Persisted       : {len(persisted):>10,}  ({len(persisted)/n_q2*100:.1f}% of {LABEL_Q2})")
    print(f"    Dropped         : {len(dropped):>10,}  ({len(dropped)/n_q2*100:.1f}% of {LABEL_Q2})")
    print(f"    New             : {len(new):>10,}  ({len(new)/n_q4*100:.1f}% of {LABEL_Q4})")

    # Tag rows
    q2 = q2.merge(
        pd.concat([persisted.assign(flow="persisted"),
                   dropped.assign(flow="dropped")], ignore_index=True),
        on=key, how="left"
    )
    q4 = q4.merge(
        pd.concat([persisted.assign(flow="persisted"),
                   new.assign(flow="new")], ignore_index=True),
        on=key, how="left"
    )

    # TSI decomposition per peril
    rows = []
    for peril in PERILS:
        p2 = q2[q2["covered_peril"] == peril]
        p4 = q4[q4["covered_peril"] == peril]

        tsi_q2      = p2["insured_value_gross"].sum()
        tsi_q4      = p4["insured_value_gross"].sum()
        pers_q2_tsi = p2[p2["flow"] == "persisted"]["insured_value_gross"].sum()
        pers_q4_tsi = p4[p4["flow"] == "persisted"]["insured_value_gross"].sum()
        new_tsi     = p4[p4["flow"] == "new"]["insured_value_gross"].sum()
        drop_tsi    = p2[p2["flow"] == "dropped"]["insured_value_gross"].sum()
        reval       = pers_q4_tsi - pers_q2_tsi

        rows.append(dict(
            peril=peril,
            tsi_q2_bn=tsi_q2 / 1e9,
            tsi_q4_bn=tsi_q4 / 1e9,
            delta_tsi_bn=(tsi_q4 - tsi_q2) / 1e9,
            new_volume_bn=new_tsi / 1e9,
            dropped_volume_bn=-drop_tsi / 1e9,
            revaluation_bn=reval / 1e9,
            pct_growth=(tsi_q4 / tsi_q2 - 1) * 100,
            n_persisted=(p2["flow"] == "persisted").sum(),
            n_new=(p4["flow"] == "new").sum(),
            n_dropped=(p2["flow"] == "dropped").sum(),
        ))

    decomp = pd.DataFrame(rows)
    show = ["peril", "tsi_q2_bn", "tsi_q4_bn", "delta_tsi_bn",
            "new_volume_bn", "dropped_volume_bn", "revaluation_bn", "pct_growth"]
    print(f"\n  TSI decomposition (EUR bn):")
    print(decomp[show].to_string(index=False, float_format="{:.2f}".format))
    decomp.to_csv(OUTPUT / f"u2_{DEEP_DIVE.lower()}_decomposition.csv", index=False)

    # Geographic centroid shift (TSI-weighted)
    print(f"\n  TSI-weighted centroid:")
    for df, label in [(q2, LABEL_Q2), (q4, LABEL_Q4)]:
        w = df["insured_value_gross"]
        print(f"    {label}: lat={np.average(df['lat'], weights=w):.5f}  "
              f"lng={np.average(df['lng'], weights=w):.5f}")

    # KS test per peril
    print(f"\n  KS test — TSI distribution Q2 vs Q4:")
    for peril in PERILS:
        v2 = q2[q2["covered_peril"] == peril]["insured_value_gross"].values
        v4 = q4[q4["covered_peril"] == peril]["insured_value_gross"].values
        ks, p = stats.ks_2samp(v2, v4)
        sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else "ns"))
        print(f"    {peril:<12}  KS={ks:.4f}  p={p:.4g}  {sig}")

    return q2, q4, decomp


# ── Section 4: Plots ──────────────────────────────────────────────────────────

def section4(q2, q4, decomp):
    print("\n── Section 4: Plots ────────────────────────────────────────────")
    sns.set_style("whitegrid")

    # 4a: Waterfall chart — ΔTSI decomposition per peril
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    for ax, row in zip(axes, decomp.itertuples()):
        steps = [
            ("Q2 TSI",   row.tsi_q2_bn,           "#4472C4", "base"),
            ("New locs", row.new_volume_bn,         "#70AD47", "delta"),
            ("Dropped",  row.dropped_volume_bn,     "#C00000", "delta"),
            ("Reval",    row.revaluation_bn,        "#ED7D31", "delta"),
            ("Q4 TSI",   row.tsi_q4_bn,             "#4472C4", "base"),
        ]

        running = 0.0
        labels, bottoms, heights, colors = [], [], [], []
        for label, val, col, kind in steps:
            if kind == "base":
                bottoms.append(0); heights.append(val)
            else:
                bot = running if val >= 0 else running + val
                bottoms.append(bot); heights.append(abs(val))
                running += val
            if label in ("Q2 TSI", "New locs"):
                running = val if label == "Q2 TSI" else running
            labels.append(label); colors.append(col)

        # Recalculate correctly
        running = 0.0
        bottoms, heights = [], []
        for label, val, col, kind in steps:
            if kind == "base":
                bottoms.append(0)
                heights.append(val)
                if label == "Q2 TSI":
                    running = val
            else:
                if val >= 0:
                    bottoms.append(running)
                else:
                    bottoms.append(running + val)
                heights.append(abs(val))
                running += val

        x = range(len(steps))
        ax.bar(x, heights, bottom=bottoms,
               color=[s[2] for s in steps], width=0.55, edgecolor="white")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_title(row.peril, fontsize=11)
        if ax is axes[0]:
            ax.set_ylabel("TSI (EUR bn)")

        # Connector dashes between intermediate bars
        prev_top = steps[0][1]
        for i in range(1, len(steps) - 1):
            ax.plot([i - 0.5, i + 0.5], [prev_top, prev_top],
                    color="grey", linewidth=0.7, linestyle="--")
            prev_top += steps[i][1]

    plt.suptitle(f"{DEEP_DIVE}: TSI decomposition {LABEL_Q2} → {LABEL_Q4}  (EUR bn)", fontsize=12)
    plt.tight_layout()
    fig.savefig(OUTPUT / f"u2_{DEEP_DIVE.lower()}_decomposition.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  → u2_{DEEP_DIVE.lower()}_decomposition.png")

    # 4b: TSI distribution comparison
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    for ax, peril in zip(axes, PERILS):
        v2 = q2[q2["covered_peril"] == peril]["insured_value_gross"] / 1e3
        v4 = q4[q4["covered_peril"] == peril]["insured_value_gross"] / 1e3
        ax.hist(v2.sample(min(40_000, len(v2)), random_state=42),
                bins=80, alpha=0.55, label=LABEL_Q2, color="#4472C4", density=True)
        ax.hist(v4.sample(min(40_000, len(v4)), random_state=42),
                bins=80, alpha=0.55, label=LABEL_Q4, color="#ED7D31", density=True)
        ax.set_title(peril)
        ax.set_xlabel("TSI (EUR k)")
        ax.legend(fontsize=8)
    plt.suptitle(f"{DEEP_DIVE}: TSI distribution {LABEL_Q2} vs {LABEL_Q4}  (40k sample)", fontsize=11)
    plt.tight_layout()
    fig.savefig(OUTPUT / f"u2_{DEEP_DIVE.lower()}_tsi_distribution.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  → u2_{DEEP_DIVE.lower()}_tsi_distribution.png")

    # 4c: HHI concentration Q2 vs Q4
    def hhi_grid(df, dim, band=0.5):
        grp = (df[dim] // band * band)
        s = df.groupby(grp)["insured_value_gross"].sum()
        sh = s / s.sum()
        return float((sh ** 2).sum())

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4))
    for ax, dim, title in [
        (ax1, "lat", "Latitude bands (0.5°)"),
        (ax2, "lng", "Longitude bands (0.5°)"),
    ]:
        q2v = [hhi_grid(q2[q2["covered_peril"] == p], dim) for p in PERILS]
        q4v = [hhi_grid(q4[q4["covered_peril"] == p], dim) for p in PERILS]
        x = np.arange(len(PERILS))
        ax.bar(x - 0.15, q2v, 0.3, label=LABEL_Q2, color="#4472C4")
        ax.bar(x + 0.15, q4v, 0.3, label=LABEL_Q4, color="#ED7D31")
        ax.set_xticks(x); ax.set_xticklabels(PERILS)
        ax.set_title(title); ax.set_ylabel("HHI"); ax.legend()
    plt.suptitle(f"{DEEP_DIVE}: spatial concentration {LABEL_Q2} vs {LABEL_Q4}", fontsize=11)
    plt.tight_layout()
    fig.savefig(OUTPUT / f"u2_{DEEP_DIVE.lower()}_concentration.png", dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  → u2_{DEEP_DIVE.lower()}_concentration.png")

    # 4d: Interactive geographic map (FLOOD peril, sampled)
    flood_peril = "FLOOD" if "FLOOD" in q2["covered_peril"].unique() else q2["covered_peril"].iloc[0]
    flood_q2 = q2[q2["covered_peril"] == flood_peril]
    flood_q4 = q4[q4["covered_peril"] == flood_peril]
    SAMPLE = 8_000

    def sample_pts(df, flow_val):
        sub = df[df["flow"] == flow_val]
        return sub.sample(min(SAMPLE, len(sub)), random_state=42)

    pers = sample_pts(flood_q2, "persisted")
    drop = sample_pts(flood_q2, "dropped")
    new  = sample_pts(flood_q4, "new")

    m = folium.Map(
        location=[flood_q2["lat"].mean(), flood_q2["lng"].mean()],
        zoom_start=7, tiles="CartoDB positron"
    )

    for df, color, name in [
        (pers, "#4472C4", f"Persisted ({len(pers):,} sampled)"),
        (drop, "#C00000", f"Dropped  ({len(drop):,} sampled)"),
        (new,  "#70AD47", f"New      ({len(new):,} sampled)"),
    ]:
        fg = folium.FeatureGroup(name=name)
        cb = f"""
            function(row) {{
                return L.circleMarker([row[0], row[1]], {{
                    radius: 3, color: '{color}',
                    fillColor: '{color}', fillOpacity: 0.7, weight: 0
                }});
            }}"""
        FastMarkerCluster(
            [[r.lat, r.lng] for r in df.itertuples()], callback=cb
        ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl().add_to(m)
    m.get_root().html.add_child(folium.Element("""
        <div style="position:fixed;bottom:30px;left:20px;z-index:999;
                    background:white;padding:10px 14px;border-radius:6px;
                    font-size:13px;box-shadow:2px 2px 6px rgba(0,0,0,.3)">
          <b>{DEEP_DIVE} — {flood_peril} portfolio (8k sample/category)</b><br>
          <span style="color:#4472C4">&#9679;</span> Persisted Q2 → Q4<br>
          <span style="color:#C00000">&#9679;</span> Dropped (Q2 only)<br>
          <span style="color:#70AD47">&#9679;</span> New (Q4 only)
        </div>"""))
    m.save(str(OUTPUT / f"u2_{DEEP_DIVE.lower()}_geographic.html"))
    print(f"  → u2_{DEEP_DIVE.lower()}_geographic.html")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng = make_engine()
    section1(eng)
    section2(eng)
    q2, q4, decomp = section3(eng)
    section4(q2, q4, decomp)
    print("\nU2 complete. All outputs in output/")
