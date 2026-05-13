"""
U1 — Co-location Graph

Builds a graph where H3 cells are nodes and edges connect cells within a
chosen radius. The radius (38 km) matches U4's characteristic clustering
scale — the distance at which German flood exposure clusters most strongly.

High-centrality nodes are locations whose loss cascades to the most
neighbouring exposure: they are the most "contagious" cells in the book.
Community detection finds natural sub-portfolios from graph structure alone,
without reference to administrative boundaries.

Two resolutions:
  res 7 (~2.3 km cells, 46K nodes) — centrality at fine scale
  res 5 (~16 km cells,  1.3K nodes) — Louvain communities (tractable)

Dataset: bldngs_ftprnts_ww_prt_2025_Q4 | country=DEU | peril=FLOOD

Outputs (output/):
  u1_coloc_centrality.png   — top-centrality cells mapped
  u1_coloc_communities.png  — Louvain community map (res 5)
  u1_coloc_degree_dist.png  — degree distribution of the graph
  u1_coloc_summary.csv      — per-cell centrality scores
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
import matplotlib.colors as mcolors
import networkx as nx
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.linalg import eigs
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

BASE_DIR   = Path(__file__).parent
OUTPUT     = BASE_DIR / "output"
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
RADIUS_KM   = 38.0   # matches U4 Ripley's L peak
RES_FINE    = 7      # centrality resolution
RES_COARSE  = 5      # community detection resolution


# ── DB ────────────────────────────────────────────────────────────────────────

def make_engine():
    return create_engine(_DB, connect_args=_CONN, pool_pre_ping=True)


# ── Section 1: load and aggregate ────────────────────────────────────────────

def section1_load(eng) -> tuple[pd.DataFrame, pd.DataFrame]:
    print(f"\n── Section 1: Load {COUNTRY} / {PERIL} and aggregate to H3 ─────")
    sql = text(f"""
        SELECT lat, lng, insured_value_gross AS tsi
        FROM "{PARTITION}"
        WHERE country = :c AND covered_peril = :p
    """)
    print("  Querying … ", end="", flush=True)
    df = pd.read_sql(sql, eng, params={"c": COUNTRY, "p": PERIL})
    print(f"{len(df):,} rows")

    for res in [RES_FINE, RES_COARSE]:
        print(f"  H3 res {res} assignment … ", end="", flush=True)
        df[f"h3_{res}"] = [h3.latlng_to_cell(lat, lng, res)
                           for lat, lng in zip(df.lat.values, df.lng.values)]
        print("done")

    def agg(res):
        cells = (df.groupby(f"h3_{res}")
                 .agg(tsi=("tsi", "sum"), n=("tsi", "count"))
                 .reset_index()
                 .rename(columns={f"h3_{res}": "cell"}))
        cells["lat"] = cells["cell"].map(lambda c: h3.cell_to_latlng(c)[0])
        cells["lng"] = cells["cell"].map(lambda c: h3.cell_to_latlng(c)[1])
        return cells.reset_index(drop=True)

    fine   = agg(RES_FINE)
    coarse = agg(RES_COARSE)
    print(f"  Res {RES_FINE}: {len(fine):,} cells  |  "
          f"Res {RES_COARSE}: {len(coarse):,} cells")
    return fine, coarse


# ── Section 2: build graph and compute centrality (res 7) ────────────────────

def section2_centrality(fine: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 2: Co-location Graph + Centrality (res {RES_FINE}, R={RADIUS_KM} km) ─")

    lat0      = fine["lat"].mean()
    km_per_lat = 111.32
    km_per_lng = 111.32 * np.cos(np.radians(lat0))

    xy = np.column_stack([fine["lng"].values * km_per_lng,
                          fine["lat"].values * km_per_lat])
    tsi = fine["tsi"].values.astype(float)
    n   = len(fine)

    print(f"  Building KD-tree ({n:,} nodes) … ", end="", flush=True)
    tree  = cKDTree(xy)
    pairs = list(tree.query_pairs(r=RADIUS_KM))
    print(f"done  ({len(pairs):,} edges)")

    # TSI-weighted sparse adjacency matrix: A[i,j] = tsi[j]
    rows = [p[0] for p in pairs] + [p[1] for p in pairs]
    cols = [p[1] for p in pairs] + [p[0] for p in pairs]
    vals = [tsi[p[1]] for p in pairs] + [tsi[p[0]] for p in pairs]
    A    = csr_matrix((vals, (rows, cols)), shape=(n, n))

    # Weighted degree: sum of neighbour TSI
    w_degree = np.array(A.sum(axis=1)).flatten()

    # Eigenvector centrality via leading eigenvector of A
    print(f"  Computing eigenvector centrality … ", end="", flush=True)
    vals_e, vecs = eigs(A.astype(float), k=1, which="LM")
    ev = np.abs(vecs[:, 0].real)
    ev = ev / ev.max()
    print("done")

    fine = fine.copy()
    fine["w_degree"]  = w_degree / 1e9        # EUR bn of neighbouring TSI
    fine["ev_central"] = ev

    fine = fine.sort_values("ev_central", ascending=False).reset_index(drop=True)
    fine.to_csv(OUTPUT / "u1_coloc_summary.csv", index=False)

    print(f"\n  Top 10 cells by eigenvector centrality:")
    top10 = fine.head(10)[["cell", "tsi", "n", "w_degree", "ev_central", "lat", "lng"]]
    top10 = top10.copy()
    top10["tsi_B"] = top10["tsi"] / 1e9
    print(top10[["tsi_B", "n", "w_degree", "ev_central", "lat", "lng"]]
          .to_string(index=False, float_format="{:.4f}".format))
    print(f"\n  → u1_coloc_summary.csv")

    return fine, pairs


# ── Section 3: community detection (res 5) ───────────────────────────────────

def section3_communities(coarse: pd.DataFrame) -> pd.DataFrame:
    print(f"\n── Section 3: Louvain Communities (res {RES_COARSE}) ─────────────────")

    lat0      = coarse["lat"].mean()
    km_per_lat = 111.32
    km_per_lng = 111.32 * np.cos(np.radians(lat0))
    xy  = np.column_stack([coarse["lng"].values * km_per_lng,
                           coarse["lat"].values * km_per_lat])
    tsi = coarse["tsi"].values.astype(float)
    n   = len(coarse)

    tree  = cKDTree(xy)
    pairs = list(tree.query_pairs(r=RADIUS_KM))

    G = nx.Graph()
    G.add_nodes_from(range(n))
    for i, j in pairs:
        G.add_edge(i, j, weight=float(tsi[i] + tsi[j]))

    print(f"  Graph: {G.number_of_nodes()} nodes  {G.number_of_edges()} edges")
    print(f"  Running Louvain … ", end="", flush=True)
    communities = nx.community.louvain_communities(G, weight="weight", seed=42)
    print(f"done  ({len(communities)} communities)")

    comm_map = {}
    for cid, members in enumerate(communities):
        for node in members:
            comm_map[node] = cid

    coarse = coarse.copy()
    coarse["community"] = coarse.index.map(comm_map)

    comm_tsi = coarse.groupby("community")["tsi"].sum().sort_values(ascending=False)
    total_tsi = coarse["tsi"].sum()
    print(f"\n  Top 5 communities by TSI:")
    for cid, tsi_val in comm_tsi.head(5).items():
        size = (coarse["community"] == cid).sum()
        print(f"    Community {cid:>2}: {size:>4} cells  "
              f"TSI {tsi_val/1e9:.1f} B EUR  ({tsi_val/total_tsi*100:.1f}%)")

    return coarse, len(communities)


# ── Section 4: plots ──────────────────────────────────────────────────────────

def section4_plots(fine: pd.DataFrame, coarse: pd.DataFrame,
                   pairs: list, n_communities: int):
    print("\n── Section 4: Plots ────────────────────────────────────────────")

    # 4a: eigenvector centrality map
    fig, ax = plt.subplots(figsize=(9, 10))
    sc = ax.scatter(fine["lng"], fine["lat"],
                    c=fine["ev_central"], cmap="YlOrRd",
                    s=fine["tsi"] / fine["tsi"].max() * 15 + 1,
                    alpha=0.7, linewidths=0)
    plt.colorbar(sc, ax=ax, label="Eigenvector centrality (normalised)")
    ax.set_title(f"{COUNTRY} — {PERIL}: Co-location eigenvector centrality\n"
                 f"(res {RES_FINE}, R = {RADIUS_KM} km, dot size ∝ cell TSI)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_coloc_centrality.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_coloc_centrality.png")

    # 4b: community map
    cmap   = plt.get_cmap("tab20", n_communities)
    colors = [cmap(c % 20) for c in coarse["community"]]

    fig, ax = plt.subplots(figsize=(9, 10))
    for cid in coarse["community"].unique():
        sub = coarse[coarse["community"] == cid]
        ax.scatter(sub["lng"], sub["lat"],
                   c=[cmap(cid % 20)], s=sub["tsi"] / sub["tsi"].max() * 40 + 3,
                   alpha=0.8, linewidths=0,
                   label=f"C{cid} ({sub['tsi'].sum()/1e9:.0f} B EUR)")

    ax.set_title(f"{COUNTRY} — {PERIL}: Louvain communities\n"
                 f"(res {RES_COARSE}, R = {RADIUS_KM} km, {n_communities} communities)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.legend(fontsize=7, loc="lower right", ncol=2, title="Community (TSI)")
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_coloc_communities.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_coloc_communities.png")

    # 4c: degree distribution
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(fine["w_degree"], bins=60, color="#4472C4", alpha=0.85)
    ax.axvline(fine["w_degree"].mean(), color="#C00000", lw=1.5, linestyle="--",
               label=f"Mean {fine['w_degree'].mean():.1f} B EUR")
    ax.set_xlabel("Weighted degree (EUR bn of neighbouring TSI within radius)")
    ax.set_ylabel("Number of cells")
    ax.set_title(f"{COUNTRY} — {PERIL}: Weighted degree distribution  (R = {RADIUS_KM} km)")
    ax.legend()
    plt.tight_layout()
    fig.savefig(OUTPUT / "u1_coloc_degree_dist.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("  → u1_coloc_degree_dist.png")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    eng = make_engine()
    fine, coarse               = section1_load(eng)
    fine, pairs                = section2_centrality(fine)
    coarse, n_communities      = section3_communities(coarse)
    section4_plots(fine, coarse, pairs, n_communities)

    print(f"\nU1 co-location graph complete. All outputs in output/")
