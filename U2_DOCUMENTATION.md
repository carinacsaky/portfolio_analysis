# U2 — Temporal Accumulation Analysis

## Purpose

Tracks how the portfolio changes between quarterly snapshots. Rather than treating each quarter as a standalone picture, U2 classifies every insured location as **persisted**, **dropped**, or **new** across snapshots and decomposes the TSI change into its three drivers: new business volume, lapsed volume, and revaluation of existing locations.

The goal is to distinguish *how* the book is growing from *whether* it is growing — a portfolio that doubles in TSI while shifting into higher-hazard regions is a different risk story from one that doubles by adding evenly distributed new locations.

---

## Current status

**The script currently runs against synthetic test data** (`building_footprints_partition`, version_uid="test_4"). The real portfolio table (`bldngs_ftprnts_ww_prt`) has 4 quarters available but access is pending. Once access is granted, two constants at the top of the script need updating — everything else runs unchanged.

| Table | Quarters | Countries | Status |
|---|---|---|---|
| `building_footprints_partition_2025_Q2` | Q2 2025 | DEU, CUB, CHE, JAM | Synthetic — methodology validation only |
| `building_footprints_partition_2025_Q4` | Q4 2025 | ~189 | Synthetic — methodology validation only |
| `bldngs_ftprnts_ww_prt_2025_Q*` | 4 quarters | 54 | Real — access pending |

---

## How to run

```bash
source .venv/bin/activate
python u2_temporal.py
```

Outputs are written to `output/`. Runtime is approximately 3–5 minutes depending on the country deep-dive size.

### Switching to real data

When access to the real portfolio table is available, confirm which quarterly partitions exist:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_name LIKE 'bldngs_ftprnts_ww_prt_%'
ORDER BY table_name;
```

Then update the two constants at the top of the script:

```python
PART_Q2 = "bldngs_ftprnts_ww_prt_2025_Q3"   # earlier quarter
PART_Q4 = "bldngs_ftprnts_ww_prt_2025_Q4"   # later quarter
```

Also remove the hardcoded row count on line 93 and replace with a real `COUNT(*)` query against `PART_Q4`.

---

## Script structure

### Section 1 — Portfolio overview

Queries the earlier snapshot (`PART_Q2`) with `GROUP BY country, covered_peril` and prints totals: row count, total TSI, number of countries. The later snapshot (`PART_Q4`) total is currently hardcoded because a full `COUNT(*)` on 1.7B rows exceeds the connection timeout.

**Output:** `u2_portfolio_overview.png` — two bar charts comparing total locations and country count between the two snapshots.

**What to look for:** A large jump in country count indicates a data onboarding event rather than organic growth. A large jump in TSI with stable country count is more likely genuine portfolio expansion.

---

### Section 2 — Country entry/exit

Classifies every country into one of three states:
- **Both quarters** — genuine temporal overlap; the only countries where flow analysis is valid
- **Earlier quarter only** — exited the book or not yet reloaded into the later snapshot
- **Later quarter only** — newly onboarded

Currently hardcoded from known metadata. When running against real data this should be replaced with a query comparing the distinct country sets between the two partition tables.

**Output:** `u2_entry_exit.csv`

**What to look for:** Countries present in both quarters are the analysis population. Countries that appear in one quarter only should be investigated — disappearance can mean cancellation, data process change, or a schema migration. It is not always real-world attrition.

---

### Section 3 — Country deep-dive (Cuba in synthetic data)

The core of the script. Runs on whichever country is present in both snapshots. When running against real data, update the `DEEP_DIVE` constant at the top to the relevant country code.

**Step 1 — Load both snapshots**

Pulls all rows for the deep-dive country from each partition: lat, lng, covered_peril, insured_value_gross, insured_value_net.

**Step 2 — Flow classification**

Identifies unique `(lat, lng, covered_peril)` triplets in each snapshot and performs an outer join. Each triplet is labelled:

| Label | Meaning |
|---|---|
| **Persisted** | Present in both snapshots |
| **Dropped** | In the earlier snapshot only — exited the book |
| **New** | In the later snapshot only — entered the book |

Matching on exact coordinate equality is appropriate because coordinates come from the same source system and are stored as double-precision floats. If the source system changes geocoding between quarters, apparent "new" and "dropped" locations may actually be the same physical locations re-geocoded — always validate this against Product G data quality diagnostics before treating flow figures as real-world events.

**Step 3 — TSI decomposition**

For each peril, decomposes the total TSI change (ΔTSI) into three components:

```
ΔTSI = new volume + dropped volume + revaluation
```

- **New volume** — sum of gross TSI for all new locations in the later snapshot
- **Dropped volume** — sum of gross TSI for all dropped locations (reported as negative)
- **Revaluation** — difference between Q4 and Q2 TSI on persisted locations only; captures indexation, mid-term endorsements, and treaty changes on the stable book

A negative revaluation on persisting locations alongside strong new volume growth means the book is growing by adding new risks, not by increasing cover on existing ones. In real data, separate the revaluation component against a known inflation index to distinguish genuine TSI reduction from indexation adjustment.

**Output:** `u2_cuba_decomposition.csv`

**Step 4 — TSI-weighted geographic centroid**

Computes the TSI-weighted mean lat/lng for both snapshots. A large centroid shift indicates the geographic centre of mass of the portfolio has moved — either through geographic expansion of new business or through selective lapsation in one region.

**Step 5 — KS test per peril**

Runs a two-sample Kolmogorov-Smirnov test comparing the distribution of individual location TSI values between the two snapshots, per peril. A significant result (p < 0.05) means the *shape* of the TSI distribution changed — not just the total — indicating that new locations have a different size profile from the existing book.

| Significance | Label |
|---|---|
| p < 0.001 | *** |
| p < 0.01 | ** |
| p < 0.05 | * |
| p ≥ 0.05 | ns |

A significant KS result for one peril but not others warrants investigation: it may mean peril-specific underwriting appetite changed, or that new locations carry a different hazard profile.

---

### Section 4 — Plots

**`u2_cuba_decomposition.png`** — Waterfall chart, one panel per peril. Starting bar is Q2 TSI; incremental bars show new volume (green, upward), dropped volume (red, downward), and revaluation (orange, up or down); final bar is Q4 TSI. Makes the decomposition visually intuitive for a non-technical audience.

**`u2_cuba_tsi_distribution.png`** — Overlaid histograms of individual location TSI values for Q2 and Q4, using a 40,000-row random sample per quarter. Shows whether the shape of the distribution changed — for example, whether new locations skew toward smaller or larger TSI than the existing book.

**`u2_cuba_concentration.png`** — Side-by-side HHI bars for each peril, Q2 vs Q4, computed on 0.5° latitude and longitude bands. Higher HHI means TSI is more concentrated in fewer geographic bands. Rising HHI over time means the book is becoming less geographically diversified.

**`u2_cuba_geographic.html`** — Interactive Folium map. Plots up to 8,000 sampled locations per category: blue for persisted, red for dropped, green for new. Open in a browser. Lets you see spatially where growth occurred and where attrition was concentrated.

---

## Interpreting the results

**The key question for each finding:**

| Finding | Ask |
|---|---|
| High % dropped | Data process change or real attrition? Check Product G. |
| Negative revaluation on persisted locations | Indexation adjustment or genuine TSI reduction? Check against inflation index. |
| Significant KS test for one peril only | Did underwriting appetite change for that peril? |
| Large centroid shift | Geographic expansion or regional lapsation? Check the geographic map. |
| HHI rising over time | Book becoming more concentrated — relevant for accumulation limits and cat XL sizing. |

---

## What 4 real quarters unlocks

With 4 quarters of real data, U2 can additionally produce:

- **3 consecutive transitions** — persisted/dropped/new classification for Q1→Q2, Q2→Q3, and Q3→Q4
- **Concentration trajectory** — HHI and Gini trending over 4 quarters
- **Velocity ranking** — fastest-growing and fastest-shrinking countries and regions
- **Drift detection** — KS and Population Stability Index (PSI) tests quarter-over-quarter
- **Entry cohort comparison** — locations entering in Q1 vs Q4, compared on TSI size, peril mix, and geography

With fewer than 3 years of data, all findings should be communicated as directional rather than predictive.

---

## Dependencies

```
psycopg2-binary
sqlalchemy
pandas
numpy
matplotlib
seaborn
scipy
folium
python-dotenv
```

Install with: `pip install -r requirements.txt` (or individually if no requirements file exists).

---

## Output files summary

| File | Type | Description |
|---|---|---|
| `u2_portfolio_overview.png` | Chart | Portfolio size Q2 vs Q4 |
| `u2_q2_summary.csv` | Table | Q2 aggregates by country and peril |
| `u2_entry_exit.csv` | Table | Country entry/exit classification |
| `u2_cuba_decomposition.csv` | Table | Per-peril ΔTSI breakdown |
| `u2_cuba_decomposition.png` | Chart | Waterfall chart of TSI decomposition |
| `u2_cuba_tsi_distribution.png` | Chart | TSI distribution Q2 vs Q4 |
| `u2_cuba_concentration.png` | Chart | Spatial HHI Q2 vs Q4 |
| `u2_cuba_geographic.html` | Interactive map | Persisted / new / dropped locations |
