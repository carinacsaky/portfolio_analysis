# Product C — Synthetic Flood Losses

## Purpose

Translates raw flood hazard depths into financial loss estimates. Where Product B overlays the hazard map and shows which locations sit in flooded zones, Product C asks: given that a location is flooded to a given depth, how much of its insured value is destroyed?

The script applies a published European residential depth-damage curve to EFAS flood depths at six return periods (RP10 through RP500), then computes portfolio-level loss metrics: gross and net loss by return period, Annual Average Loss (AAL) via trapezoidal integration, and a geographic breakdown of the worst 20 H3 cells.

This is the first script in the portfolio analysis chain to produce a number a reinsurer recognises — a loss in euros at a specified return period — rather than a purely descriptive exposure metric.

---

## How to run

```bash
source .venv/bin/activate
python c_synthetic_losses.py
```

Runtime is approximately 10–20 minutes. The slowest step is section 2: sampling six rasters of continental Europe against 5.7M coordinate pairs. All outputs are written to the `output/` directory.

### Changing country or peril

At the top of the script:

```python
COUNTRY = "DEU"    # any 3-letter ISO code present in the partition
PERIL   = "FLOOD"  # currently FLOOD (EFAS rasters are flood-specific)
```

The raster paths are also defined at the top:

```python
RASTERS = {
    "RP10":  "/home/carina/Downloads/floodMap_RP010/floodmap_EFAS_RP010_C.tif",
    ...
    "RP500": "/home/carina/Downloads/floodMap_RP500/floodmap_EFAS_RP500_C.tif",
}
```

All six rasters must be present and readable before the script is run. They are not downloaded by the script.

---

## Script structure

### Section 1 — Load data

Pulls `lat`, `lng`, `insured_value_gross` (TSI), and `insured_value_net` from the real portfolio table (`bldngs_ftprnts_ww_prt_2025_Q4`) for all locations matching the specified country and peril. Both gross and net values are loaded here so that net loss and ceded loss can be computed downstream without a second database query.

For DEU / FLOOD this returns approximately 5.7M rows.

---

### Section 2 — Raster sampling

Reprojects all portfolio coordinates from WGS84 (EPSG:4326) to LAEA Europe (EPSG:3035) — the native CRS of the EFAS flood maps — using `pyproj`. Samples each of the six GeoTIFF rasters at the reprojected coordinates using `rasterio`.

**Flood detection:** a pixel is treated as flooded if its value is greater than zero and greater than the no-data threshold (−1e30). All other pixels receive depth 0.0. This means locations outside the flood extent contribute zero loss, which is correct: they are exposed to the peril but not flooded at that return period.

**Raster values:** EFAS rasters report inundation depth in metres. Values above 6 m are valid — they represent locations in river channels or deep valley floors. The damage curve caps at 100% above 6 m, so these locations are treated as total losses regardless of exact depth.

Output columns added: `depth_RP10`, `depth_RP20`, `depth_RP50`, `depth_RP100`, `depth_RP200`, `depth_RP500`.

---

### Section 3 — Depth-damage to gross loss per location

Applies the Huizinga et al. (2017) European residential piecewise-linear depth-damage curve to each location's flood depth at each return period.

**Damage curve (Huizinga et al. 2017):**

| Depth (m) | 0 | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 | 4.0 | 5.0 | 6.0 |
|---|---|---|---|---|---|---|---|---|---|
| Damage factor (%) | 0 | 10 | 30 | 45 | 55 | 70 | 80 | 85 | 100 |

The curve is interpolated linearly between control points using `numpy.interp`. A single generic curve is used for all buildings — there is no building-type or occupancy split because occupancy is not available in the current schema.

**Loss computation:**

```
gross_loss = TSI_gross × damage_factor(depth)
net_loss   = TSI_net   × damage_factor(depth)
```

The same damage factor is applied to both gross and net TSI. This is equivalent to a proportional (quota share) treaty structure where the net-to-gross ratio is constant at the location level.

Output columns added per return period: `dmg_factor_RP*`, `loss_RP*`, `loss_net_RP*`.

---

### Section 4 — Portfolio metrics and AAL

Aggregates location-level losses to portfolio totals, computes gross and net loss ratios, and integrates the OEP curve to estimate Annual Average Loss.

**AAL calculation:**

Trapezoidal integration over the six-point OEP curve anchored at annual rates 1/10, 1/20, 1/50, 1/100, 1/200, and 1/500. Two boundary adjustments are applied:

- **Head (below RP10):** loss ramps linearly from 0 at an annual rate of 1.0 to L(RP10) at rate 1/10. This contributes a triangular area of `0.5 × L(RP10) × (1.0 − 1/10)`.
- **Tail (beyond RP500):** held constant at L(RP500) with a weight of 1/500. This underestimates tail risk but is conservative by design given the absence of modelled events beyond RP500.

**DEU FLOOD gross losses:**

| Return period | Gross loss | Net loss | Ceded |
|---|---|---|---|
| RP10 | 33.2 B EUR | 22.2 B EUR | 11.0 B EUR |
| RP20 | 40.8 B EUR | 27.2 B EUR | 13.6 B EUR |
| RP50 | 49.5 B EUR | 33.0 B EUR | 16.5 B EUR |
| RP100 | 55.4 B EUR | 37.0 B EUR | 18.4 B EUR |
| RP200 | 61.3 B EUR | 40.9 B EUR | 20.4 B EUR |
| RP500 | 69.1 B EUR | 46.1 B EUR | 23.0 B EUR |

**AAL summary:**

- Gross AAL: 19.3 B EUR (0.75% of gross TSI)
- Net AAL: 12.9 B EUR
- Ceded AAL: 6.4 B EUR (33% of gross AAL)

**Net/gross ratio:** constant at 66.7% across all return periods. This is the signature of a proportional quota share treaty: the reinsurer takes a fixed share of every loss regardless of size. No excess-of-loss layer is visible in the data — if an XL treaty were in place, the net/gross ratio would rise sharply at higher return periods as losses breach the attachment point.

Results are written to `output/c_loss_summary.csv`.

---

### Section 4b — Top 20 H3 cells by RP100 gross loss

Aggregates location-level RP100 gross losses to H3 resolution 7 hexagonal cells (approximately 2.3 km across, matching the cell size used in U1 and U4). Only locations with a positive RP100 loss are included in the grouping.

Per-cell metrics: number of locations, total TSI (M EUR), mean flood depth (m), loss ratio (%), gross loss (M EUR), net loss (M EUR), and the cell centroid coordinates.

**Top cell (Berlin, Spree/Havel basin):** 52.54°N 13.32°E — 1,102 locations, 490 M EUR TSI, 392 M EUR RP100 gross loss (loss ratio 80%). The top 20 cells are dominated by Berlin (Spree and Havel rivers) and Frankfurt am Main (Main river).

The H3 resolution 7 grid is used rather than a finer resolution because it produces cells large enough to contain meaningful clusters of buildings while remaining small enough to resolve intra-city spatial patterns. Coarser resolutions would merge distinct river basins.

Results are written to `output/c_top_cells.csv`.

---

### Section 5 — Plots

**`c_oep_curve.png`**

Gross and net OEP curves on a log-scale return-period axis (10–500 years). Each point is annotated with the loss value in B EUR. The area between the gross and net curves is shaded red to show the ceded layer. AAL values for gross and net appear in the legend. The parallel shape of the two curves confirms the proportional treaty structure.

**`c_depth_damage.png`**

The Huizinga et al. (2017) European residential depth-damage curve plotted as a continuous line with the nine control points marked. Intended as a methodology reference — to include in reports or review sessions where the damage assumption needs to be explained or challenged.

**`c_loss_map.png`**

Location-level estimated gross loss at RP100 plotted as a scatter map over Germany. Loss values are colour-coded on a YlOrRd scale, capped at the 99.5th percentile to prevent extreme channel values from washing out the colour range. Grey points are locations with zero RP100 loss (outside the flood extent). This is the primary geographic deliverable for a claims or underwriting audience.

---

## Interpreting the results

**Loss ratio as a sanity check**

The RP100 gross loss ratio (55.4 B / ~2,584 B TSI ≈ 2.1%) is the fraction of the entire DEU FLOOD book that would be destroyed in a 1-in-100 year event. This is the number to anchor board-level and regulatory discussions. Compare it against industry benchmarks and internal cat model outputs to assess whether the synthetic estimate is conservative or aggressive.

**Constant net/gross ratio signals treaty structure**

If the net/gross ratio were constant across return periods, the reinsurance programme is purely proportional. If it rose with return period, an XL layer is in force. The 66.7% constant ratio observed here is consistent with a quota share: the reinsurer cedes 33.3% of every loss at every size. No XL protection is visible. This should be cross-checked against the actual treaty schedule.

**Top cells as accumulation hotspots**

The top-20 H3 cells from section 4b are the locations where a single flood event causes the most total loss. Berlin and Frankfurt dominating the list reflects two factors: large volumes of insured buildings in the river floodplain, and EFAS assigning deep flood depths to those cells at RP100. These cells are candidates for per-risk limit reviews or facultative reinsurance placement.

**Cell loss ratios above 70%**

A cell-level loss ratio of 80% (as in the top Berlin cell) means that if a 1-in-100 year flood materialises, nearly all of the insured value in that hexagon is expected to be destroyed. This is not a modelling artefact — the EFAS depth at those locations is high (>4 m) and the damage curve correctly assigns near-total destruction at those depths. Deep cells are channel-adjacent properties.

**Depths above 6 m**

EFAS raster values can exceed 6 m for locations in or immediately adjacent to river channels. The damage curve caps at 100% at 6 m, so these locations are modelled as total losses. In practice, such locations are unlikely to be insurable at standard terms. Flagging them separately (e.g. filtering `depth_RP100 > 6`) can identify locations that may warrant individual underwriting review.

---

## Extensions

**Add building-type splits**

Once occupancy codes become available in the schema, replace the single generic curve with type-specific curves from the Huizinga et al. (2017) dataset (residential, commercial, industrial). Industrial buildings typically have higher contents values relative to structure, shifting the loss profile.

**Uncertainty on the damage curve**

The Huizinga et al. (2017) report provides uncertainty bounds around the control points. Run section 3 using the upper and lower bound curves in addition to the central estimate to produce a loss range rather than a point estimate. This is a standard sensitivity that reinsurers expect in technical submissions.

**Net/gross ratio by layer**

Replace the single proportional multiplier with an explicit per-location or per-policy XL structure. Load attachment, limit, and participation per location from the treaty schedule and compute net loss as `min(max(gross_loss − attachment, 0), limit) × participation`. The resulting net OEP curve will diverge from the gross curve at the attachment point, revealing the effective protection.

**Other perils and countries**

The script structure is peril-agnostic. To run for a different country, change `COUNTRY` and update `RASTERS` to point to the corresponding national or European hazard maps. For non-flood perils (storm surge, earthquake ground shaking), replace the EFAS rasters with the appropriate intensity metric and update the damage curve accordingly.

**Temporal trend**

If earlier quarters of the portfolio are available, run the full pipeline for each quarter and track AAL and the RP100 loss over time. A rising AAL indicates accumulation growth in flooded zones — relevant for cat budget planning.

---

## Dependencies

```
psycopg2-binary
sqlalchemy
pandas
numpy
matplotlib
rasterio
pyproj
h3
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

The EFAS flood rasters (GeoTIFF, EPSG:3035) must be downloaded separately and their paths set in the `RASTERS` dictionary at the top of the script. They are not bundled with the repository.

---

## Output files summary

| File | Type | Description |
|---|---|---|
| `output/c_loss_summary.csv` | Table | Gross loss, net loss, loss ratios by return period, plus AAL row |
| `output/c_top_cells.csv` | Table | Top 20 H3 res-7 cells by RP100 gross loss with coordinates, TSI, depth, and LR |
| `output/c_oep_curve.png` | Chart | Gross and net OEP curve on log RP axis with ceded shading and AAL labels |
| `output/c_depth_damage.png` | Chart | Huizinga et al. (2017) depth-damage curve with control points (methodology reference) |
| `output/c_loss_map.png` | Chart | Location-level estimated gross loss at RP100 plotted over Germany |
