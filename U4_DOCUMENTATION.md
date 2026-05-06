# U4 — Scale-Invariance Diagnostics

## Purpose

Measures portfolio concentration at every spatial scale simultaneously. Standard accumulation analysis (Product A) tells you *how much* TSI is concentrated — U4 tells you *at what geographic distance* that concentration lives.

This matters because catastrophe events have physical footprints. A windstorm covers a different radius than a flood, which covers a different radius than an earthquake. If the portfolio clusters at exactly the scale of a peril's typical event footprint, a single event hits the book harder than aggregate TSI figures suggest. U4 makes that scale-dependency explicit and quantified.

---

## How to run

```bash
source .venv/bin/activate
python u4_scale_invariance.py
```

Runtime is approximately 5–8 minutes. The slowest step is the 6,000×6,000 pairwise distance matrix in section 3.

### Changing country or peril

At the top of the script:

```python
COUNTRY  = "DEU"    # any 3-letter ISO code present in the partition
PERIL    = "FLOOD"  # FLOOD, FIRE, or EARTHQUAKE
```

Re-running for different country/peril combinations and comparing their concentration curves is itself a useful analysis — see the Extensions section below.

---

## Script structure

### Section 1 — Load data

Pulls lat, lng, and gross TSI for all locations matching the specified country and peril from the real portfolio table (`bldngs_ftprnts_ww_prt_2025_Q4`). For DEU / FLOOD this is 5.7M rows and 2,584B EUR gross TSI.

---

### Section 2 — Multi-scale HHI/Gini

The main deliverable. Computes concentration metrics at 6 spatial scales using H3 hexagonal grid cells.

**How H3 works:** H3 divides the Earth into hexagonal cells at discrete resolution levels. Each resolution is approximately 7× finer in area than the previous. At resolution 3 each cell covers ~111 km; at resolution 8 each cell covers ~0.9 km.

| Resolution | Scale (km) | Cells (DEU FLOOD) | HHI | Gini | Eff N |
|---|---|---|---|---|---|
| 3 | 111 | 49 | 0.036 | 0.480 | 28 |
| 4 | 42 | 246 | 0.008 | 0.482 | 132 |
| 5 | 16 | 1,266 | 0.001 | 0.451 | 692 |
| 6 | 6 | 7,466 | 0.0003 | 0.482 | 3,632 |
| 7 | 2.3 | 46,179 | 0.00006 | 0.607 | 15,997 |
| 8 | 0.9 | 209,809 | 0.00002 | 0.677 | 60,649 |

**Performance note:** The script computes the finest resolution (res 8) from raw coordinates using `latlng_to_cell`, then derives all coarser resolutions using `cell_to_parent` — a bitwise operation that is orders of magnitude faster than recomputing from coordinates.

**Concentration metrics computed:**

- **HHI** (Herfindahl-Hirschman Index) — sum of squared TSI shares across cells. Ranges from near 0 (perfectly even) to 1 (all TSI in one cell). Effective N = 1/HHI gives the equivalent number of equal-sized cells.
- **Gini coefficient** — measures inequality of TSI distribution across cells. 0 = perfectly equal; 1 = all TSI in one cell.
- **Top-1% and top-5% share** — percentage of total TSI in the single largest cell and the 5 largest cells respectively.

**Reading the results:**

The shape of HHI and Gini across scales tells you where concentration risk actually lives:

| Curve shape | Meaning |
|---|---|
| Flat across all scales | Scale-invariant (fractal-like); events at any scale produce comparable losses |
| Rising steeply at small scales, flattening at large | Local clustering dominates; neighbourhood or city-block risk |
| Rising steeply at large scales, flat at small | Regional concentration; country or basin-level dominance |

For DEU / FLOOD: HHI drops smoothly as scale decreases (concentration is real at every scale). Gini stays flat at ~0.48 from res 3–6, then jumps sharply to 0.68 at res 8. This means within-neighbourhood TSI inequality is much higher than regional inequality — some street blocks are extremely dense while adjacent ones are nearly empty.

**Output:** `u4_summary.csv`, `u4_concentration_curve.png`

---

### Section 3 — Ripley's L function

A point-process statistical test that asks: are these locations more clustered than if they had been placed at random, and at what distance?

**How it works:**

1. Takes a TSI-weighted sample of 6,000 locations. Locations with higher TSI are more likely to be sampled, so the sample is representative of exposure concentration, not just location density.
2. Projects coordinates to kilometres using a flat-earth approximation centred on the sample's mean latitude.
3. Computes all pairwise distances between the 6,000 points (18M pairs).
4. For each radius r in a log-spaced sequence from 0.5 km to 250 km, counts how many pairs fall within that distance.
5. Computes Ripley's K(r) — the observed neighbour density at radius r scaled by the study area, compared to a completely spatially random (CSR) process.
6. Linearises to L(r) = sqrt(K(r) / π) − r, where the CSR baseline becomes zero.

**Reading the result:**

| L(r) value | Interpretation |
|---|---|
| L(r) > 0 | More clustered than random at scale r |
| L(r) < 0 | More dispersed than random at scale r |
| L(r) ≈ 0 | Indistinguishable from random scatter at scale r |

The radius where L(r) peaks is the **characteristic clustering scale** — the distance at which locations cluster most strongly relative to a random baseline.

**DEU / FLOOD result:** Peak at **~38 km**. This matches the typical width of major German river basins (Rhine, Elbe, Danube tributaries). Below ~5 km the portfolio is close to random. Above ~100 km clustering weakens, indicating genuine regional diversification.

**Insurance implication:** A flood event with a ~40 km footprint hits this portfolio harder than the raw TSI map suggests. This number is directly relevant to per-risk and cat XL sizing decisions.

**Output:** `u4_ripley_L.png`

---

### Section 4 — Correlation dimension

Summarises the entire spatial clustering geometry of the portfolio as a single number D.

**How it works:**

Uses the same pairwise distances from section 3. For each radius r, computes C(r) — the fraction of all location pairs that fall within distance r of each other (the correlation integral). Then fits a straight line through log(C(r)) vs log(r). The slope of that line is the correlation dimension D.

**What D means:**

| D value | Interpretation |
|---|---|
| D ≈ 2 | Locations scattered randomly across a flat 2D surface |
| D ≈ 1 | Locations aligned along a 1D structure (river, road, coastline) |
| 1 < D < 2 | Clustered but not purely linear — pulled toward linear features |
| D < 1 | Extremely tight point clusters |

**DEU / FLOOD result:** D = 1.61 (R² = 1.000, fit range 22–209 km). The portfolio behaves like a 1.6-dimensional object — meaningfully clustered and organised around linear geographic features (river valleys, transport corridors) without being purely linear. The near-perfect R² confirms the power law holds cleanly across nearly a decade of spatial scales.

**Reinsurance relevance:** D tells a reinsurer how quickly accumulated exposure grows as an event footprint expands. A lower D means exposure grows more slowly with radius — useful for per-risk XL pricing. A higher D (closer to 2) means near-uniform density and faster accumulation.

**Output:** `u4_correlation_dim.png`

---

### Section 5 — Plots

**`u4_concentration_curve.png`**

Three panels sharing a log-scale x-axis (spatial scale in km):
- Top: HHI vs scale
- Middle: Gini vs scale
- Bottom: Effective N (1/HHI) vs scale

Each data point is labelled with its H3 resolution and cell count. This is the primary deliverable for a portfolio management or reinsurance audience.

**`u4_ripley_L.png`**

L(r) − r plotted against radius on a log scale. Blue shading above zero indicates clustering; red shading below zero indicates dispersion. The orange dashed vertical line marks the peak clustering radius (~38 km for DEU / FLOOD).

**`u4_correlation_dim.png`**

Log-log plot of C(r) against r. The fitted line is shown in red with the slope D annotated. The fitting range (22–209 km) is shaded — this is the range where C(r) is between 2% and 80% of its maximum, avoiding edge effects at both extremes.

---

## Interpreting the results

**The key questions for each finding:**

| Finding | Ask |
|---|---|
| Gini flat across large scales but high at fine scales | Where exactly is within-neighbourhood inequality? Map res 7 and res 8 cells. |
| Ripley's L peak at a specific radius | Does that radius match the typical footprint of the dominant peril? If yes, the portfolio is structurally exposed at that event scale. |
| D close to 1 | Which linear features drive the clustering? Overlay with river network or road network. |
| D close to 2 | Exposure is spread evenly — diversification at all scales is genuine. |
| HHI rising over time (when quarterly data is available) | Book becoming more concentrated — relevant for accumulation limits. |

---

## Extensions

**Run across multiple countries**

Comparing the concentration curve for DEU vs NLD vs GBR on FLOOD, or DEU FLOOD vs DEU EARTHQUAKE, shows whether the characteristic clustering scale is peril-driven (river basins for flood, fault lines for EQ) or book-composition-driven (urban density, client type).

**Add a null model**

Generate a random point cloud with the same bounding box, density, and TSI distribution as the real data, run sections 3 and 4 on it, and plot alongside the real results. Makes "how clustered is this really" concrete rather than relative to a theoretical baseline.

**Temporal version**

When earlier quarters are available, run U4 on each quarter and track how D and the Ripley's L peak radius change over time. A D value drifting downward (toward 1) means the portfolio is becoming more linearly concentrated — a risk signal worth flagging to underwriting.

**Hazard overlay split**

Split locations into high-hazard and low-hazard zones using JRC flood return-period maps (freely available). Run U4 separately on each half. If high-hazard locations have a higher D or a lower Ripley's L peak radius, the concentrated TSI is sitting in the most hazard-exposed areas — evidence of adverse geographic selection.

---

## Dependencies

```
psycopg2-binary
sqlalchemy
pandas
numpy
matplotlib
scipy
h3
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

---

## Output files summary

| File | Type | Description |
|---|---|---|
| `u4_summary.csv` | Table | Per-resolution HHI, Gini, eff-N, top-1%, top-5% |
| `u4_concentration_curve.png` | Chart | HHI, Gini, eff-N vs log(scale) — main deliverable |
| `u4_ripley_L.png` | Chart | L(r) − r vs radius |
| `u4_correlation_dim.png` | Chart | log C(r) vs log r with power-law fit and D value |
