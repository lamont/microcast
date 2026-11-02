# Live bridge forecast · spec

*2026-10-25. Replaces phase-1 Next #4 ("live inference, first cut"), narrowed
to the Golden Gate Bridge. Design agreed in conversation 2026-10-19; not yet
built. Nothing here changes training or the backtest.*

## Why

The user rides across the Golden Gate Bridge. Before a ride the question is
*how hard will the wind shove me sideways at mid-span over the next few hours,
and when is it calmest?* Crosswind gust is the main thing. Rain is rare but
worth knowing about, and so is a sharp drop in temperature (the marine layer
pushing through). Today that answer takes a hand-run scratch script (the
2026-10-07 and 2026-10-19 spikes); this makes it a command and a site section.

## Done means

1. `uv run microcast forecast` prints the bridge's next 6 hours, starting from
   the newest HRRR run that's out, in about the format below, and saves the
   result.
2. `uv run microcast site` puts that saved result at the top of the status
   page: a one-glance verdict, with the hourly table under it.
3. The site and the CLI show the same numbers. The site reads what the
   command saved and never recomputes.
4. Re-running the command is safe and light enough (under 1 GB) for Dagster
   to run it hourly in the run window, 08:00–21:00 Pacific, before the site
   is rebuilt and published (D12; `dagster-k8s.md`, "One schedule").

## What it shows

**Point:** GGBC1, the station at mid-span on the deck (82 m). Speeds in mph,
temperatures in °F, local times.

**Window:** the next 6 hours from the newest HRRR cycle, which is
`gold.MAX_LEAD_MIN` and what the models are trained and scored on. Nothing
past 6 h.

**Per hour:**

| Column | Meaning |
|---|---|
| time | local, valid hour |
| xgust | expected crosswind gust (see "Crosswind" below) |
| 80% | the 80% range of crosswind gust |
| >22 | chance crosswind gust exceeds 22 mph (the heads-up level) |
| rain | `–`, or a mark when HRRR expects measurable rain that hour |
| temp | corrected temperature |

**Levels** (set by the user against the 2026-07-13 crossing: 23–25 mph
crosswind gusts around 4–6 PM were "rideable, not dangerous, but unpleasant
and hard to fight; rounding the tower columns the wind hit like a fist"; less
wind is always preferred). They describe effort, not danger:

| Crosswind gust | Label | Daylight frequency at GGBC1, 2025-04 → 2026-10 |
|---|---|---|
| < 18 | calm enough | most hours |
| 18–22 | breezy | ~18% of readings ≥ 20 |
| 22–25 | heads-up: a real fight | 10% of readings (the 90th percentile); ~1 day in 2 |
| ≥ 25 | July 13: unpleasant, hard work | 3.5% of readings; ~1 day in 5 |

Keep the numbers in one place (config or a module constant), since the user may
tune them.

**The sentence** (CLI and site): calmest stretch in the window first (the real
decision is *when* to go), then the windiest hour and its chance of crossing
22, then rain or temperature only if they fire. For example: "Calmest before
noon. Breezy from 1–4 PM, peaking at 18 mph at 1 PM with a ~1 in 4 chance of
gusts over 22. No rain, no temperature drop."

**Flags.**
- Rain: HRRR `prate` (collected, unused until now), raw. *My call:* flag an
  hour at ≥ 0.25 mm/h (about the "measurable" line); tune on first sight.
- Temperature drop: corrected `t2m`. *My call:* flag when any hour in the
  window is ≥ 5 °F below now (the latest obs, else the first hour).

**Now line:** the bridge's latest observation (gust, direction, temperature)
*with its time*. That time is about 75 minutes behind (below), so it has to be
shown honestly, not labelled "now".

**Provenance line:** HRRR cycle, latest GGBC1 obs time, model name, build time.
Make it plain when the forecast is stale, e.g. the newest cycle more than 2 h
old.

**Small print, once, on the site:** GGBC1 is at mid-span; gusts rounding the
towers are locally stronger than the station sees.

### CLI mock (agreed)

```
$ microcast forecast
Golden Gate Bridge mid-span (GGBC1) · HRRR 17Z · obs 10:15 PDT · gbm_terrain_stack
last obs: gust 17 mph from 241°, 62°F
Calmest before noon. Breezy 1-4 PM, peak 18 mph at 1 PM (~1 in 4 chance over 22). No rain, no temperature drop.

 time    xgust  80%     >22   rain  temp
 11 AM    15    12-19     2%    -    61°F
 12 PM    17    12-21     8%    -    61°F
  1 PM    18    14-23    17%    -    62°F
 ...
```

(The numbers are illustrative, from the 2026-10-19 17Z spike.)

### Site layout (agreed: mockup "C with B")

At the **top of the status page** (`site/index.html`), above the backtest
sections:

1. Verdict block: the big number is the window's peak crosswind gust, with its
   hour beneath it, and the sentence beside it.
2. The hourly table (the CLI's columns), each row tinted by level.
3. Provenance line and the tower small print.

No new nav tab. The mockups are under `.superpowers/brainstorm/` (not committed).

## How it's made

Build on the existing code and on what the 2026-10-19 spike proved. Use the
same functions as training, so live features match what the model learned.

**Inputs, each run:**
- **HRRR:** the newest live cycle via `ingest nwp` (`nwp.fetch_cycle`, Herbie,
  leads 0–6 at least). GGBC1 is a registry point, so it's already covered. The
  hrrrzarr archive lags ~3 h, so it can't be the source. The lagged-ensemble
  features need the previous 6 cycles' forecasts for the same hours. Those
  come from bronze if the hourly runs have been landing; otherwise fetch them
  too, t2m and gust, leads up to 12.
- **Obs:** `ingest madis` (the live real-time files; Synoptic ended
  2026-10-19, D10). GGBC1 arrives ~75 min late. Gold's `OBS_LATENCY_MIN`
  already makes training use the hour-old obs, so live inference does the
  same: the same as-of rule, no special case.
- **Upwind features:** the MADIS stations at the cycle's issue time, as
  `transform.upwind` builds them.
- **Static features:** `silver.static_features`, unchanged.

**Features:** build silver and gold rows for the live cycle(s) *in memory*
with `silver.build_*` and `gold.build_training`, over a recent window of
bronze (≥ 14 days, for the rolling bias). Then join static and upwind as
`pipeline.load_gold` does. Don't write silver or gold from this command; the
lake's one-writer-per-table rule (D12) stays simple. Factor out of
`load_gold` whatever join logic is needed rather than copying it.

**Model:** `gbm_terrain_stack` for both `gust` and `t2m`, the default for
points with their own obs (2026-10-18). **Decided (2026-11-01): fit once a
day, not each run.** Fitting on all of gold costs ~1 min per target and ~5 GB
peak (the spike), which would make every hourly run a heavy one. The fit is
its own step (`forecast_model` in `dagster-k8s.md`, in the daily chain after
`build gold`); it saves both boosters under `/data/models/` with the gold
snapshot they were fitted on, written whole and renamed. `microcast forecast`
loads the newest pair and only predicts, and says which snapshot it used in
the provenance line. With no saved model it stops with a message naming the
fit command; it doesn't fit on its own. A CLI form of the fit
(`microcast forecast --fit`) keeps it runnable without Dagster.

**Crosswind (interim, until Next #3):** the bridge axis is 354.7°. The
crosswind share is `|sin(dir − 354.7°)|`, from HRRR's 10 m wind direction for
that hour. Crosswind gust is `mu_gust × share`, with the range and chance
scaled the same way: P(xgust > 22) = P(gust > 22 / share). The corrected
gust's distribution comes from the model's Gaussian (mu, sigma). When Next #3
lands a trained `xgust` target, it replaces this without changing the
output.

**Saved result:** one record per run, holding the inputs' provenance, the
hourly rows, the sentence and the flags. `site` reads the latest one.
**Decided (the user, 2026-11-01): keep every run**, in an append-only table
(e.g. `gold.live_forecasts`), so forecasts can later be scored against what
GGBC1 measured, unless it becomes too expensive on disk. Rough size: 6 hourly
rows per run, hourly through daylight, is a few KB of Parquet per run, so
single-digit MB a month; the real cost is one small file and one Iceberg
snapshot per run, so give the table the same metadata pruning as bronze and
compact it monthly. Revisit only if it passes ~1 GB.

**Writes and the catalog:** `ingest nwp` and `ingest madis` append to bronze,
so they follow the one-writer rule. The forecast step itself only reads,
plus the saved result, under its own pool (`gold.live_forecasts`), so the
daily backtest never holds it up.

## Not in scope

- The house, the Castro stations, the Golden Gate Park ride. The original
  Next #4 listed them; they come later. The park ride also needs a corrected
  wind, since raw HRRR is far too high there.
- A trained crosswind target and a `gg_bridge` route (Next #3).
- Anything past 6 h, push notifications, a separate bridge page.
- The Dagster assets (`forecast`, `forecast_model`). These commands are what
  they call (`dagster-k8s.md`).

## Must not happen

- The site and the CLI disagreeing.
- A stale forecast that doesn't look stale.
- Raw HRRR shown as if it were corrected. Rain is raw HRRR; say so where it
  shows.
- Live features built differently from training's (a silent skill loss).

## Checks

- Unit tests: levels, the sentence (calm, breezy, rain, temperature-drop cases),
  crosswind share and the scaled exceedance chance.
- Replay: for a past cycle already in gold, the live path's feature rows equal
  gold's rows for that cycle (the as-of guarantee).
- A real run, compared by eye with GGBC1's next few hours of obs.

## Related findings from the same session

- Silver at GGBC1 holds four bogus gusts of 110–133 mph on 2026-09-25 around
  05:00 PDT, with no sustained wind and the vane at due north. Gold drops them,
  because gust needs `wind10`, but silver QC let them through: a test case for
  Next #5.
- Weekly maximum crosswind gust at GGBC1 is usually 25–33 mph. The extremes
  were 51 mph (Feb 2026) and 37–40 mph in winter and spring storms.
