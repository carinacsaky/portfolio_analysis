# Product A — Accumulation & Concentration Analysis

## Purpose

Provides a full top-down view of portfolio accumulation: how much TSI sits in each country, how evenly it is distributed across countries, and where the heaviest concentrations are located geographically. It answers the first questions a portfolio manager or cat modeller asks when looking at a book — "where is the exposure, how concentrated is it, and how much reinsurance protection is the net-to-gross ratio providing?"

Product A covers the broadest scope of any script in this project. It spans the entire portfolio (349M rows, 54 countries, 3 perils, 157T EUR gross TSI), produces country-level concentration metrics, maps the deep-dive country at two spatial resolutions (H3 hex grid and NUTS3 administrative regions), examines how the net-to-gross ratio varies across perils, and delivers an interactive bubble-map choropleth for use in presentations or dashboards.

---

## How to run

```bash
source .venv/bin/activate
python a_accumulation.py
```

Runtime is approximately 10–20 minutes end to end. The dominant cost is the spatial join in section 4 (5.7M DEU locations against NUTS3 polygons). Section 1 runs a full GROUP BY on the 349M-row table, which takes several minutes on the database side.

### Changing the deep-dive country

At the top of the script:

```python
DEEP_DIVE = "DEU"   # any 3-letter ISO code present in the partition
NUTS_CODE = "DE"    # matching 2-letter ISO code for the NUTS3 geometry table
```

Sections 3 and 4 use only these two constants. Change both and re-run to produce the H3 and NUTS3 maps for a different country. Section 1, 2, 5, and 6 always cover the full 54-country portfolio regardless of `DEEP_DIVE`.

---

## Script structure

### Section 1 — Portfolio inventory (A.1)

Runs a single `GROUP BY country, covered_peril` query on the full partition table. Only streaming aggregates are used (COUNT, SUM, MIN, MAX). `PERCENTILE_CONT` was deliberately excluded because it requires a full sort of the 349M-row table and reliably causes SSL timeout at this scale.

Results are rolled up to country level (summing across perils) and sorted by gross TSI descending. The top 10 countries are printed to console.

**Key headline figures:**
- 54 countries, 3 perils, 157T EUR gross TSI
- DEU is the largest country at roughly 25% of total TSI
- `avg_net_gross` shows the average retention ratio per country — watch for outliers at either extreme (near 0 = nearly fully ceded; near 1 = very little reinsurance cover)

**What to look for:** Countries near the top of `a_inventory_by_country.csv` with a low `avg_net_gross` are heavily reinsured; those with a high ratio carry most of the risk net. A sudden change in this ratio between reporting periods signals a treaty change.

**Output:** `a_inventory.csv` (raw country × peril), `a_inventory_by_country.csv` (rolled up)

---

### Section 2 — Concentration metrics (A.3 / A.7)

Computes three concentration metrics across countries for each peril separately.

**Metrics computed:**

- **HHI** (Herfindahl-Hirschman Index) — sum of squared TSI shares. Near 0 = perfectly even; 1 = all TSI in one country. Effective N = 1/HHI gives the equivalent number of equal-weight countries.
- **Gini coefficient** — inequality of TSI across countries. 0 = perfectly equal; 1 = complete monopoly.
- **Theil T** — entropy-based measure, more sensitive to changes at the top of the distribution than Gini.

**Known results (all-peril combined):** HHI = 0.0587, Gini = 0.645, eff_n ≈ 17. These figures mean the portfolio behaves as if it were spread across roughly 17 equal countries — modest diversification given 54 are represented. DEU alone accounts for approximately 25% of TSI, which drives HHI up significantly.

**What to look for:** Compare HHI and Gini across the three perils. A peril with noticeably higher HHI than the others means that peril's exposure is more geographically concentrated — relevant for per-peril cat XL sizing. Top5 and Top10 share figures show whether concentration is driven by a single dominant country (steep top-1) or a cluster.

**Output:** `a_concentration.csv` (metrics per peril), `a_concentration.png` (HHI and Gini bar charts), `a_top_countries.png` (horizontal bar chart of top 20 countries with n/g ratio on secondary axis)

---

### Section 3 — H3 spatial grid — DEU (A.2)

Loads all DEU FLOOD locations (lat, lng, gross TSI) from the database and bins them into H3 hexagonal cells at resolutions 5, 6, 7, and 8 (cell diameters of approximately 16 km, 6 km, 2.3 km, and 0.9 km respectively).

**Performance note:** H3 cells are assigned at the finest resolution needed (res 8) using `latlng_to_cell`. Coarser resolutions are derived with `cell_to_parent`, a bitwise operation that is far faster than recomputing from coordinates.

For each resolution the script prints: number of occupied cells, HHI, effective N, and the share of TSI in the single top cell. These numbers describe intra-country spatial concentration — a complement to the country-level metrics in section 2.

A scatter plot is produced at resolution 7 (46k occupied cells for DEU FLOOD). Point colour encodes log(TSI) per cell; point size encodes location count. This gives a quick visual of where the heavy accumulations sit within Germany.

**What to look for:** A large jump in HHI between adjacent resolutions (e.g. res 6 → res 7) indicates that TSI inequality is concentrated at that spatial scale. The top-cell share at res 8 flags whether a single ~1 km hex holds a disproportionate fraction of the country's exposure — a potential single-risk accumulation issue.

**Output:** `a_h3_deu.png`

---

### Section 4 — NUTS3 aggregation — DEU (A.2)

Joins the DEU FLOOD location points against NUTS3 administrative boundaries retrieved from the `nuts3_eu_admin` database table. Points are projected from WGS84 (EPSG:4326) to the same CRS as the NUTS geometry (EPSG:3857) before the spatial join.

After joining, TSI is summed per NUTS3 region. The script prints the NUTS3-level HHI and effective N, then lists the top 5 NUTS3 regions by TSI. A choropleth map is produced with a yellow-orange-red colour ramp.

**What to look for:** Compare the NUTS3 HHI to the H3 res-5 HHI from section 3. They should be in the same range (both represent a 15–20 km spatial scale). If they diverge, the mismatch reflects the irregular shape of NUTS3 boundaries versus the uniform H3 grid. The top-5 NUTS3 table directly identifies the administrative districts carrying the highest concentration — useful for discussing accumulation limits with underwriting.

**Output:** `a_nuts3_deu.png`

---

### Section 5 — Cross-peril analysis (A.6)

Examines how the net-to-gross (n/g) ratio varies across perils within each country. A country where the n/g ratio differs by more than 1 percentage point between FLOOD, FIRE, and EARTHQUAKE has peril-specific reinsurance structures — meaning the gross-to-net transformation is not uniform across the book.

The script identifies all such countries and prints the top 10 by magnitude of variation. It then plots the n/g ratio for FLOOD across the top 30 countries by gross TSI, with a red dashed mean line for reference.

The cross-peril breakdown also exposes the exposure mix across FLOOD, FIRE, and EARTHQUAKE at country level. Countries that appear in the inventory for only one or two perils (rather than all three) may indicate selective underwriting or data gaps.

**What to look for:** Countries with a high n/g range across perils indicate that cession rates are being applied selectively by peril — common where treaty structures differ (e.g. earthquake cover written on a separate treaty from flood). These are worth flagging in model governance reviews. The FLOOD n/g chart quickly shows which large countries are retaining more versus ceding more.

**Output:** `a_netgross_ratio.png`

---

### Section 6 — Interactive choropleth (A.8)

Builds a Folium bubble map showing gross TSI by country as circles on an interactive world map. Bubble area is proportional to gross TSI (radius scales as √TSI so visual area is proportional to value). Hovering over a bubble shows country code, gross TSI in EUR bn, location count, and average n/g ratio.

Country centroids are derived from two sources: NUTS3 centroids computed from `nuts3_eu_admin` for EU countries, and hardcoded lat/lng coordinates for the four non-EU countries in the portfolio (AUS, IDN, JPN, PHL). Countries whose centroids cannot be resolved are silently dropped.

The map uses a CartoDB Positron base tile for a clean presentation-ready appearance.

**What to look for:** The interactive map is primarily a communication tool. The spatial clustering of large bubbles in Western Europe (DEU, FRA, CHE, AUT) versus the isolated bubbles in Asia-Pacific makes the geographic concentration of the book immediately legible to a non-technical audience. The tooltip values let you cross-check specific countries during a review without consulting the CSV.

**Output:** `a_choropleth.html` (open in any browser)

---

## Interpreting the results

**Key questions for each finding:**

| Finding | Ask |
|---|---|
| HHI = 0.0587, eff_n = 17 at country level | Which 2–3 countries account for the excess concentration above what 17 equal countries would imply? (Answer: DEU plus the next 2–3 largest.) |
| Gini = 0.645 | Over half the TSI inequality is explained by the top few countries. Is this consistent with client mix expectations or a sign of book imbalance? |
| DEU ~25% of TSI | One country drives one-quarter of total exposure. What is the maximum probable loss for a single severe DEU event, and does the net-of-reinsurance position make it acceptable? |
| Large spread in NUTS3 top-5 vs bottom regions | Are the top NUTS3 districts consistent with known urban centres (Munich, Frankfurt, Hamburg), or are there unexpected outliers that warrant a data quality check? |
| Countries with n/g variation >1pp across perils | Are the peril-specific treaties documented? If not, the net position for that country may be misunderstood. |
| Isolated large bubble in choropleth (e.g. JPN) | Verify that a single non-EU country concentration is intentional and covered by an appropriate cat programme. |

---

## Extensions

**Run for other deep-dive countries**

Change `DEEP_DIVE` and `NUTS_CODE` to any country with a NUTS3 entry (all EU member states, plus Norway, Switzerland, and others). Comparing H3 concentration curves for DEU vs FRA vs ITA on the same peril separates book-composition effects from underlying geographic effects.

**Add approximate row counts via pg_class**

For a fast sanity check of table size without a full COUNT(*), query `pg_class.reltuples` or use `TABLESAMPLE BERNOULLI(1)` to estimate row counts. Both are safe on the 349M-row table without triggering timeouts. Example:

```sql
SELECT reltuples::bigint AS approx_rows
FROM pg_class
WHERE relname = 'bldngs_ftprnts_ww_prt_2025_Q4';
```

**Add per-peril H3 maps**

Section 3 currently runs only FLOOD. Repeating it for FIRE and EARTHQUAKE and overlaying the three heat maps shows whether the spatial footprints of the three perils co-locate or diverge — a direct input to multi-peril accumulation analysis.

**Lorenz curve**

Plot the cumulative TSI share against cumulative country count (sorted ascending) to produce a Lorenz curve. Shading the area between it and the 45-degree equality line gives a visual representation of the Gini coefficient and shows at a glance where the distribution departs most from equality.

**Temporal trend**

When earlier quarters are available, run the full script on each quarter and track how HHI, Gini, and the top-country share change over time. Creeping concentration (rising HHI) is an early warning signal for accumulation management.

**Peril-split choropleth**

Modify section 6 to produce three separate Folium maps (one per peril) or a single map with a layer toggle. This makes it possible to identify countries that are large in one peril but small in another — relevant for multi-peril XL structuring.

---

## Dependencies

```
psycopg2-binary
sqlalchemy
pandas
numpy
geopandas
matplotlib
seaborn
h3
folium
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

The script also requires access to the `nuts3_eu_admin` table in the portfolio database. This table must contain columns `nuts_id`, `name_latn`, `cntr_code`, `levl_code`, and a PostGIS geometry column `geom` in EPSG:3857.

---

## Output files summary

| File | Type | Description |
|---|---|---|
| `a_inventory.csv` | Table | Row count and TSI per country × peril combination |
| `a_inventory_by_country.csv` | Table | TSI, location count, and avg n/g ratio per country, sorted by gross TSI |
| `a_concentration.csv` | Table | HHI, Gini, Theil, eff-N, top-5%, top-10% per peril |
| `a_concentration.png` | Chart | HHI and Gini bar charts by peril |
| `a_top_countries.png` | Chart | Top 20 countries by gross TSI (horizontal bars) with n/g ratio on secondary axis |
| `a_h3_deu.png` | Chart | Scatter map of DEU FLOOD accumulation at H3 resolution 7 |
| `a_nuts3_deu.png` | Chart | DEU choropleth of gross TSI per NUTS3 administrative region |
| `a_netgross_ratio.png` | Chart | Net-to-gross ratio by country for FLOOD (top 30 by TSI) |
| `a_choropleth.html` | Interactive map | Folium bubble map — radius proportional to √TSI, tooltip on hover |
