# U1 — Building Graph & Co-location Graph

## Purpose

Identifies two distinct types of hidden correlation within the portfolio that geographic aggregation alone cannot detect.

**Building stacks** (`u1_building_graph.py`) finds locations where multiple policies cover the same physical building under the same peril. A single event — a fire, a flood — hitting that building triggers every stacked policy simultaneously. The standard view of "number of locations" overstates diversification: what looks like 3 separate risks is actually 1 building with 3 exposure entries. This script quantifies how much of the book is in that situation, how concentrated the stacked exposure is, and where the highest-risk stacks sit.

**Co-location graph** (`u1_colocation_graph.py`) operates at the regional scale. It builds a spatial graph where H3 cells are nodes and edges connect any two cells within 38 km of each other — the characteristic flood clustering scale identified by U4's Ripley's L analysis. Eigenvector centrality then identifies the cells whose losses are most likely to cascade across the book; Louvain community detection partitions the portfolio into natural sub-portfolios based purely on geographic proximity, without reference to administrative boundaries.

Together the two scripts map intra-building correlation (the micro scale) and inter-region correlation (the meso scale).

---

## How to run

```bash
source .venv/bin/activate
python u1_building_graph.py
python u1_colocation_graph.py
```

Both scripts are self-contained. They read country and peril from constants at the top of each file.

### Changing country or peril — building graph

```python
COUNTRY = "DEU"   # any 3-letter ISO code present in the partition
```

The building graph loads all perils for the selected country and reports stacks per peril separately. To restrict to a single peril, add a `WHERE covered_peril = :p` clause to the SQL in `section1_load`.

### Changing country or peril — co-location graph

```python
COUNTRY   = "DEU"
PERIL     = "FLOOD"
RADIUS_KM = 38.0   # adjust if U4 gives a different Ripley's L peak for your country/peril
```

If you run U4 on a different country or peril first, update `RADIUS_KM` to match that run's Ripley's L peak before running the co-location graph. The 38 km value is specific to DEU / FLOOD.

### Runtime

- `u1_building_graph.py`: approximately 2–4 minutes. The slowest step is the initial database query.
- `u1_colocation_graph.py`: approximately 6–10 minutes. The slowest step is `tree.query_pairs` which finds all 16M+ pairs within the 38 km radius.

---

## Script structure: u1_building_graph.py

### Section 1 — Load data

Pulls lat, lng, covered_peril, and gross TSI for all rows matching the selected country from `bldngs_ftprnts_ww_prt_2025_Q4`. All perils are loaded together so that multi-peril buildings can be handled correctly. For DEU this is the full policy population across FLOOD, FIRE, and EARTHQUAKE.

---

### Section 2 — Genuine stack detection

The core method. Converts lat/lng to a discrete 20-metre grid:

```python
y_cell = (lat  * 111_320      / 20).round().astype(int)
x_cell = (lng  * m_per_lng    / 20).round().astype(int)
```

`m_per_lng` adjusts for latitude: `111_320 * cos(lat_mean_radians)`. The 20 m tolerance absorbs geocoding jitter between policies on the same building.

Each unique `(cell, covered_peril)` pair is treated as one building-peril combination. A genuine stack is any such pair with 2 or more policies. This design correctly excludes single buildings with multi-peril coverage: a building with FLOOD and FIRE policies produces two cell-peril groups, each with n=1, and neither is flagged as a stack.

**DEU results:**
- 714,835 genuine stacks
- 655.7 B EUR stacked TSI (8.5% of total DEU TSI)
- Predominantly pairs: 688,721 stacks of exactly 2 policies
- Maximum single-address TSI: 3.66 M EUR (6 policies, FIRE)

Output: `u1_stack_summary.csv` — one row per genuine stack with policy count, total TSI, centroid lat/lng, and peril.

---

### Section 3 — Concentration metrics

Applies HHI to the stacked TSI values to quantify how concentrated the stacks themselves are. Effective N = 1/HHI gives the equivalent number of equally-sized stacks.

Also computes a Lorenz curve and top-N share analysis (top 10, 100, 1,000, and 10,000 stacks as a share of all stacked TSI). These are plotted together in `u1_concentration.png`.

The HHI here is directly comparable to the effective-N figures from Product A. The gap between "effective geographic zones" and "effective buildings" quantifies how much apparent geographic diversification is illusory — the book is more concentrated at the individual building level than the zone map suggests.

---

### Section 4 — Plots

Three visualisations are produced:

**`u1_stack_stats.png`** — Two panels: left shows the distribution of stack sizes (how many policies per building per peril); right shows the distribution of stack TSI values on a log y-axis.

**`u1_top_stacks.png`** — Horizontal bar chart of the top 20 genuine stacks by TSI. Each bar is labelled with the stack's centroid coordinates, peril, and policy count.

**`u1_stack_map.html`** — Folium interactive map showing the top 500 stacks by TSI as circles. Circle radius scales as `(tsi / max_tsi)^0.5 * 25`, with a minimum radius of 4. Hovering shows peril, policy count, and TSI in EUR M.

---

## Script structure: u1_colocation_graph.py

### Section 1 — Load and aggregate to H3

Loads lat, lng, and gross TSI for the selected country and peril. Assigns each row to two H3 resolutions simultaneously:

- **Resolution 7** (~2.3 km cells, ~46K nodes for DEU FLOOD) — used for centrality
- **Resolution 5** (~16 km cells, ~1.3K nodes for DEU FLOOD) — used for community detection

Assignment uses `h3.latlng_to_cell`; cell centroids are recovered via `h3.cell_to_latlng`. TSI is summed and policy counts aggregated within each cell at each resolution.

Using two resolutions is a deliberate trade-off: eigenvector centrality on 46K nodes requires a sparse matrix solver but gives fine spatial detail; Louvain on 46K nodes would be tractable but its communities would be too fragmented to interpret, so the coarser resolution is used instead.

---

### Section 2 — Co-location graph and eigenvector centrality (res 7)

Builds the spatial graph at resolution 7.

**KD-tree construction:** Cell centroids are projected to kilometres using a flat-earth approximation centred on the mean latitude. `scipy.spatial.cKDTree.query_pairs(r=38.0)` finds all pairs of cells within the 38 km radius. For DEU FLOOD this produces 16.3M edges.

**Sparse adjacency matrix:** A `scipy.sparse.csr_matrix` of shape (n, n) is built where `A[i, j] = tsi[j]`. The weight of an edge from cell i to cell j equals the TSI of cell j — representing how much exposure at j is reachable from i in one step. The matrix is symmetric.

**Weighted degree:** Row sums of A give each cell's total neighbouring TSI within the radius (reported in EUR bn).

**Eigenvector centrality:** Computed via `scipy.sparse.linalg.eigs(A, k=1, which="LM")` — the leading eigenvector of the adjacency matrix. Unlike degree, eigenvector centrality rewards being connected to high-centrality cells, not just being connected to many cells. Scores are normalised to [0, 1] by dividing by the maximum.

**DEU FLOOD result:** Top centrality cell near Stuttgart (48.83°N, 9.17°E).

Output: `u1_coloc_summary.csv` — per-cell TSI, policy count, weighted degree, and eigenvector centrality score, sorted descending by centrality.

---

### Section 3 — Louvain community detection (res 5)

Builds a NetworkX graph at resolution 5. Edge weights are `tsi[i] + tsi[j]`, so the Louvain algorithm is guided by the combined exposure of neighbouring cells rather than purely by connectivity.

`networkx.community.louvain_communities(G, weight="weight", seed=42)` partitions the nodes into communities by maximising weighted modularity. `seed=42` ensures reproducibility.

**DEU FLOOD results:**
- 18 Louvain communities
- Top community (C17): 82 cells, 301.5 B EUR (11.7% of DEU FLOOD TSI)

The community labels (C0–C17) are assigned by descending TSI of the community, so C0 is always the largest by exposure.

---

### Section 4 — Plots

Three visualisations are produced:

**`u1_coloc_centrality.png`** — Scatter plot of all res-7 cells coloured by eigenvector centrality (yellow-orange-red scale). Dot size scales with cell TSI. High-centrality cells near major river confluences and urban flood zones appear in deep red.

**`u1_coloc_communities.png`** — Scatter plot of res-5 cells coloured by Louvain community membership (tab20 palette). Dot size scales with cell TSI. The legend lists each community's total TSI in EUR bn. Communities correspond roughly to river basin sub-catchments and urban agglomerations.

**`u1_coloc_degree_dist.png`** — Histogram of weighted degree (EUR bn of neighbouring TSI within 38 km) across all res-7 cells. The mean is marked with a dashed red line. The heavy right tail identifies the cells with the most correlated neighbourhood exposure.

---

## Interpreting the results

### Building stacks

| Finding | Ask |
|---|---|
| High TSI in stacks (>5% of total) | Is this TSI properly accumulation-limited? Per-building sub-limits may not be applied. |
| Stacks concentrated in a few locations | Map the top stacks — are they in a single district or spread nationally? High geographic concentration of stacks amplifies event scenarios. |
| Large max stack (many policies, high TSI) | Is the maximum single-building TSI within per-risk XL retention? A stack above the retention that is not flagged as co-located is a hidden exposure. |
| Peril breakdown of stacks | If FIRE stacks dominate, the fire accumulation model may be understating event loss. If FLOOD stacks dominate, the same applies to cat models. |
| Lorenz curve bowing sharply | Most stacked TSI is in a small number of buildings — effective-N is low and the tail risk is concentrated. |

### Co-location graph

| Finding | Ask |
|---|---|
| High eigenvector centrality cell | A single event footprint centred on this cell reaches the most TSI in the book. What is the gross loss estimate for a 1-in-200 event centred here? |
| Top centrality cell near Stuttgart (DEU FLOOD) | Stuttgart sits at the confluence of several Rhine tributaries. Cross-check with the hazard overlay from Product B. |
| Community with >10% of total TSI | A basin-level event could exhaust this community's contribution to cat XL in a single event. Size the community against per-region accumulation limits. |
| Many small communities (fragmented) | The book is well-diversified across basins at the chosen radius. Verify this holds at a larger radius (e.g. 100 km) by rerunning with `RADIUS_KM = 100`. |
| Heavy-tailed degree distribution | Most cells are lightly connected but a small number carry disproportionate neighbourhood exposure — consistent with the Gini findings from U4. |

---

## Extensions

**Connect building stacks to the co-location graph**

Assign each genuine stack from `u1_stack_summary.csv` to its res-7 H3 cell and join to `u1_coloc_summary.csv`. Stacks that fall in high-centrality cells are doubly dangerous: within-building correlation AND neighbourhood correlation apply simultaneously.

**Vary the radius**

Rerun `u1_colocation_graph.py` with `RADIUS_KM` set to the Ripley's L peak for a different peril (e.g. FIRE or EARTHQUAKE). Communities and centrality rankings will shift — comparing across perils shows whether the same physical locations dominate under different event types.

**Multi-country comparison**

Run both scripts for DEU, FRA, and NLD on FLOOD. Comparing the share of TSI in genuine stacks, and comparing the eigenvector centrality maps, shows whether stack risk or co-location risk is book-composition artefact or peril-driven.

**Track stacks over time**

When earlier quarterly partitions are available, run `u1_building_graph.py` on each quarter and track the count of genuine stacks and their total TSI. An increasing trend indicates that building-level accumulation is not being managed actively.

**Overlay communities with cat model zones**

Export res-5 community boundaries and overlay with the insurer's or reinsurer's internal zone definitions. Misalignment between Louvain communities and zone boundaries indicates that the zone system does not reflect the actual correlation structure of the book.

**Aggregate centrality to res-5 for combined view**

Compute the mean or maximum res-7 eigenvector centrality within each res-5 community. This gives a single risk score per community that combines size (TSI) with connectedness — useful for prioritising which communities to investigate in depth.

---

## Dependencies

```
psycopg2-binary
sqlalchemy
pandas
numpy
matplotlib
folium
scipy
h3
networkx
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

`networkx` is required only by `u1_colocation_graph.py`. `folium` is required only by `u1_building_graph.py`.

---

## Output files summary

### u1_building_graph.py

| File | Type | Description |
|---|---|---|
| `output/u1_stack_summary.csv` | Table | Per genuine stack: policy count, TSI, centroid lat/lng, peril |
| `output/u1_stack_stats.png` | Chart | Stack size distribution (left) and TSI distribution log-scale (right) |
| `output/u1_top_stacks.png` | Chart | Top 20 stacks by TSI — horizontal bar chart |
| `output/u1_concentration.png` | Chart | Lorenz curve of stacked TSI (left) and top-N share bars (right) |
| `output/u1_stack_map.html` | Interactive map | Top 500 stacks by TSI, circle radius proportional to TSI |

### u1_colocation_graph.py

| File | Type | Description |
|---|---|---|
| `output/u1_coloc_summary.csv` | Table | Per res-7 cell: TSI, policy count, weighted degree (EUR bn), eigenvector centrality |
| `output/u1_coloc_centrality.png` | Chart | Eigenvector centrality map at res-7, dot size proportional to cell TSI |
| `output/u1_coloc_communities.png` | Chart | Louvain community map at res-5, one colour per community |
| `output/u1_coloc_degree_dist.png` | Chart | Weighted degree distribution with mean marked |
