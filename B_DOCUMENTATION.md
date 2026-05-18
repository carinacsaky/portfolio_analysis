# Product B — Flood Hazard Overlay

## Purpose

Determines how much of the portfolio sits inside EFAS flood hazard zones, broken down by return period and flood depth. The output is a hazard-weighted TSI exposure profile: for each return period, how many locations are in the floodplain, what percentage of gross TSI they represent, and how that TSI is distributed across flood depth bands.

This differs from Product A (accumulation) in that it uses an external physical hazard layer rather than self-referential concentration. A location can be a concentration risk (many co-located peers) without being a flood hazard risk, and vice versa. Product B answers the question directly relevant to cat pricing and reinsurance purchasing: how much of the book floods, and at what depth, if a 1-in-100-year or 1-in-500-year event occurs?

---

## How to run

```bash
source .venv/bin/activate
python b_hazard_flood.py
```

Runtime is approximately 3–6 minutes. The slowest step is the raster sampling loop in section 2, which opens each of the 6 EFAS rasters and queries ~5.7 million coordinates.

### Changing country or peril

At the top of the script:

```python
COUNTRY = "DEU"    # any 3-letter ISO code present in the partition
PERIL   = "FLOOD"  # FLOOD, FIRE, or EARTHQUAKE
```

### Adding or changing return periods

The `RASTERS` dictionary at the top of the script maps return period labels to local raster file paths:

```python
RASTERS = {
    "RP10":  "/home/carina/Downloads/floodMap_RP010/floodmap_EFAS_RP010_C.tif",
    ...
    "RP500": "/home/carina/Downloads/floodMap_RP500/floodmap_EFAS_RP500_C.tif",
}
```

Add, remove, or reorder entries here to change which return periods are processed. All downstream sections consume `RASTERS` dynamically — no further changes are needed.

---

## Script structure

### Section 1 — Load locations

Pulls lat, lng, and gross TSI for all rows matching the specified country and peril from the live portfolio table (`bldngs_ftprnts_ww_prt_2025_Q4`). For DEU / FLOOD this is approximately 5.7 million rows.

---

### Section 2 — Raster sampling

The core computational step. Reprojects portfolio coordinates from WGS84 (EPSG:4326) to EPSG:3035 (ETRS89 / Lambert Azimuthal Equal Area Europe) — the native CRS of the EFAS rasters — then samples each raster at those projected coordinates using `rasterio`.

**Coordinate reprojection:**

Uses `pyproj.Transformer` with `always_xy=True`. The reprojection is performed once and the EPSG:3035 coordinates are reused for all return periods:

```python
transformer = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
x3035, y3035 = transformer.transform(df["lng"].values, df["lat"].values)
```

**EFAS raster properties:**

| Property | Value |
|---|---|
| Source | EFAS (European Flood Awareness System / Copernicus) |
| CRS | EPSG:3035 (ETRS89 / LAEA Europe) |
| Pixel resolution | 100 m |
| NoData value | ~-3.4e38 (IEEE 754 minimum float) |
| Pixel value | Water depth in metres at the given return period |
| Licence | Freely available from Copernicus / JRC |

**Flooded condition:**

A location is classified as flooded if the sampled raster value satisfies:

```python
flooded = (vals > 0) & (vals > NODATA_THRESHOLD)   # NODATA_THRESHOLD = -1e30
```

The two conditions combined exclude both nodata fill values (large negative floats) and cells that technically returned zero or negative depth (dry land or raster artefacts). Flooded pixels receive their depth in metres stored in `depth_<RP>`; non-flooded pixels receive 0.0.

**Output columns added to `df` per return period:**

- `depth_RP10`, `depth_RP20`, … — sampled water depth (0.0 for not-flooded)
- `flooded_RP10`, `flooded_RP20`, … — boolean flag

---

### Section 3 — TSI exposure by hazard band

Aggregates the sampled flags and depths into exposure metrics per return period.

**Depth bands:**

| Band | Range |
|---|---|
| 0–0.5 m | Shallow inundation; usually recoverable damage |
| 0.5–1 m | Significant structural exposure; rising damage curve |
| 1–2 m | Severe; most ground-floor contents and structure at risk |
| 2–5 m | High; multi-storey flood damage likely |
| >5 m | Extreme; total-loss territory |

For each return period the section prints and records:

- Number and percentage of flooded locations
- Total and percentage of TSI in the floodplain
- TSI breakdown by depth band (both absolute EUR and share of flooded TSI)

Results are written to `b_flood_summary.csv`.

**DEU / FLOOD results:**

| Return period | Flooded locations | % of total | Flooded TSI (B EUR) | % of total TSI |
|---|---|---|---|---|
| RP10 | 193,000 | 3.4% | 87.2 | 3.4% |
| RP100 | 288,000 | 5.0% | 129.9 | 5.0% |
| RP500 | 342,000 | 6.0% | 154.2 | 6.0% |

---

### Section 4 — Plots

Produces two output charts.

**4a: `b_flood_exposure.png` — stacked bar chart by depth band**

Each bar corresponds to one return period. The bar is stacked into five depth-band segments (light to dark blue: shallow to deep). The y-axis shows percentage of total gross TSI. Each bar is annotated at the top with the aggregate flooded TSI percentage and absolute EUR value.

The chart makes two things immediately visible: how total flood exposure grows with return period, and how the depth composition changes. At low return periods (RP10) the flooded TSI is predominantly in shallow bands; at high return periods (RP500) the share of deep flooding (>2 m) increases meaningfully, reflecting that larger events not only cover more area but push water deeper.

**4b: `b_flood_map.png` — geographic scatter plot**

Maps all portfolio locations for the first entry in `RASTERS` (RP10 by default), colouring flooded locations blue and non-flooded grey. Useful for a quick sanity check of which river corridors and coastal zones are flagged, and to identify any spatial artefacts from the reprojection or raster sampling.

---

## Interpreting the results

**TSI% ≈ location% across all return periods**

For DEU / FLOOD the fraction of TSI in the floodplain is nearly identical to the fraction of locations in the floodplain at every return period (both approximately 3.4% at RP10, 5.0% at RP100, 6.0% at RP500). This indicates no systematic adverse selection: the average insured value of a building in the floodplain is not materially higher than outside it. If TSI% were consistently above location%, it would suggest the book is overweight on high-value properties in flood-exposed areas.

**Depth mix shift at higher return periods**

As the return period increases, the share of flooded TSI in deeper bands (>2 m, >5 m) grows. This matters for loss estimation: a 1-in-500 event is not just 1.75× larger than a 1-in-100 event in terms of exposed TSI — it also hits exposed properties harder on average, compounding the loss ratio effect.

**Key questions for follow-up:**

| Finding | Ask |
|---|---|
| TSI% well above location% | Which property types or sub-regions drive the over-representation? Filter by zone or construction class. |
| TSI% well below location% | Is the book actively avoiding high-value flood-zone properties, or is this incidental? |
| Large jump in >5 m band at RP200/RP500 | Which sub-regions are driving deep flooding? May warrant per-location or per-zone sublimits. |
| Geographic map shows unexpected clusters | Check whether raster sampling is using the correct CRS or whether nodata handling is masking real exposure. |

---

## Extensions

**Split by sub-region or construction type**

The SQL query in section 1 can be extended with additional columns (e.g. federal state, construction material, occupancy class). Section 3 can then compute the depth-band breakdown per sub-group, making it possible to identify whether flood exposure is concentrated in a specific region or building segment.

**Compute expected annual loss (EAL)**

With six return periods available, it is straightforward to approximate an EAL curve using the trapezoidal rule on the exceedance probability axis. Apply damage curves per depth band (typically sourced from JRC or industry tables) to convert flooded TSI into estimated losses at each RP, then integrate. This is the natural next step toward a simplified internal flood cat model.

**Add a damage-curve weighting**

Rather than treating all flooded TSI equally, weight each flooded location's contribution by a depth-damage factor that maps water depth to fraction of value lost. Even a simple step function (5% at <0.5 m, 15% at 0.5–1 m, 35% at 1–2 m, etc.) substantially improves the loss relevance of the output compared to raw TSI counts.

**Extend to other countries**

Set `COUNTRY` to any ISO code present in the database and re-run. The EFAS rasters cover all of Europe, so no additional hazard data is needed for other EU/EEA markets.

**Overlay with Product A accumulation zones**

Joining the flood flag back onto the accumulation zones from Product A identifies which high-concentration zones are also high flood-hazard zones. This intersection is the most direct measure of correlated risk: locations that are simultaneously dense and flooded drive cat model outputs disproportionately.

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
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

**Raster data:** EFAS flood depth maps for RP10, RP20, RP50, RP100, RP200, RP500 are freely downloadable from the Copernicus Emergency Management Service / JRC. Place each extracted `.tif` file in the corresponding path under `RASTERS` before running.

---

## Output files summary

| File | Type | Description |
|---|---|---|
| `b_flood_summary.csv` | Table | Per-return-period count of flooded locations, % flooded, total and flooded TSI in B EUR, % TSI flooded |
| `b_flood_exposure.png` | Chart | Stacked bar: % of total TSI in floodplain by depth band and return period — main deliverable |
| `b_flood_map.png` | Chart | Geographic scatter of flooded vs not-flooded locations at RP10 |
