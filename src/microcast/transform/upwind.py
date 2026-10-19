"""Silver -> gold.upwind_features: the marine push as the MADIS stations see it, per HRRR cycle.

What the PurpleAir transect gave (changes moving inland from Ocean Beach,
``transform.network``), from stations we already pull for free. The
exploration behind it (spec, "Upwind changes"): on afternoons with a push at
the coast, a push follows on the first ridge 75-90 min later (odds x6) and
on the city's east side 1.5-3.5 h later (x2-3).

Stations fall in three groups along the push's path (``GROUPS``). For each
group and each of temperature and RH, the group mean of every station's
own 1 h and 3 h change and its anomaly against its trailing 14-day mean at
the same time of day. Changes are averaged per station first, so a station
dropping out doesn't move the mean. Then coast minus east (the push in one
number, as ``pa_west_east_anom`` was) and ``up_push_age_min``: minutes since
today's first coastal push (a station's temperature down 2 C and RH up 8
points within an hour, 11:00-22:59 local), empty if none yet.

As-of rule: obs are averaged into 10-minute buckets; a cycle uses the bucket
ending at its init time, visible well before the issue time (init + 55 min).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
from pyiceberg.expressions import AlwaysTrue, And, EqualTo, In

from microcast.lake.catalog import get_catalog, replace_partition
from microcast.lake.schemas import NETWORK_FEATURES_SCHEMA

GROUPS = {
    "coast": ("604PG", "D5422", "AV684", "F8022"),  # west end of Golden Gate Park, Pacifica
    "ridge": ("PG800", "070PG", "G0189", "F2543"),  # Daly City hills, Twin Peaks south, Corona Heights
    "east": ("C5988", "E9227", "E5830", "F4637", "F6803", "F8548", "G3010", "G6226", "438PG"),  # Castro to Bayview
}
BUCKET = "10min"
PUSH_DT, PUSH_DRH = -2.0, 8.0
PUSH_HOURS = (11, 22)
LOCAL_TZ = "America/Los_Angeles"
ANOM_DAYS, ANOM_MIN_DAYS = 14, 7


def _wide(obs: pd.DataFrame, variable: str) -> pd.DataFrame:
    """10-minute bucket means, one column per station, on a regular index."""
    o = obs[obs.variable == variable]
    if o.empty:
        return pd.DataFrame()
    t = pd.to_datetime(o.obs_time, utc=True).dt.floor(BUCKET)
    w = o.assign(t=t).pivot_table(index="t", columns="station_id", values="value", aggfunc="mean")
    return w.asfreq(BUCKET)


def _anomaly(w: pd.DataFrame) -> pd.DataFrame:
    """Value minus the mean of the previous ``ANOM_DAYS`` days at the same 10-minute slot."""
    slot = w.index.hour * 6 + w.index.minute // 10
    clim = w.groupby(slot).transform(lambda s: s.shift(1).rolling(ANOM_DAYS, min_periods=ANOM_MIN_DAYS).mean())
    return w - clim


def _push_onsets(t2m: pd.DataFrame, rh: pd.DataFrame) -> pd.Series:
    """First coastal push per local day: Series of onset times indexed by local date."""
    coast = [s for s in GROUPS["coast"] if s in t2m and s in rh]
    if not coast:
        return pd.Series(dtype="datetime64[ns, UTC]")
    dt = t2m[coast] - t2m[coast].shift(6)
    drh = rh[coast] - rh[coast].shift(6)
    hit = ((dt <= PUSH_DT) & (drh >= PUSH_DRH)).any(axis=1)
    local = hit.index.tz_convert(LOCAL_TZ)
    hit &= (local.hour >= PUSH_HOURS[0]) & (local.hour <= PUSH_HOURS[1])
    on = hit.index[hit.to_numpy()]
    return pd.Series(on, index=on.tz_convert(LOCAL_TZ).date).groupby(level=0).min()


def build_features(obs: pd.DataFrame) -> pd.DataFrame:
    """Long (init_time, feature, value) for every whole hour the obs cover."""
    t2m, rh = _wide(obs, "t2m"), _wide(obs, "rh2m")
    if t2m.empty:
        return pd.DataFrame(columns=["init_time", "feature", "value"])
    rh = rh.reindex(t2m.index) if not rh.empty else pd.DataFrame(index=t2m.index)
    per = {}
    for var, w in (("t2m", t2m), ("rh", rh)):
        stats = {"d1h": w - w.shift(6), "d3h": w - w.shift(18), "anom": _anomaly(w)}
        for group, ids in GROUPS.items():
            cols = [s for s in ids if s in w]
            for stat, frame in stats.items():
                per[f"up_{group}_{var}_{stat}"] = frame[cols].mean(axis=1) if cols else np.nan
    f = pd.DataFrame(per, index=t2m.index)
    for var in ("t2m", "rh"):
        f[f"up_coast_east_{var}_anom"] = f[f"up_coast_{var}_anom"] - f[f"up_east_{var}_anom"]
    # A cycle at init uses the bucket that ends at init: label init - 10 min.
    f.index = f.index + pd.Timedelta(BUCKET)
    f = f[f.index.minute == 0]
    onsets = _push_onsets(t2m, rh)
    day = f.index.tz_convert(LOCAL_TZ).date
    onset = pd.to_datetime(pd.Series(day, index=f.index).map(dict(onsets.items())), utc=True)
    age = (f.index.to_series() - onset).dt.total_seconds() / 60
    # The onset is stamped with its bucket's start; it is seen once that bucket has ended.
    f["up_push_age_min"] = age.where(age >= 10)
    long = f.rename_axis("init_time").reset_index().melt(id_vars="init_time", var_name="feature", value_name="value")
    return long.dropna(subset=["value"]).sort_values(["init_time", "feature"]).reset_index(drop=True)


def stations() -> list[str]:
    return sorted({s for ids in GROUPS.values() for s in ids})


def rebuild() -> int:
    """Rebuild gold.upwind_features in full from silver; returns the row count."""
    obs = (
        get_catalog()
        .load_table("silver.obs_qc")
        .scan(
            row_filter=And(In("station_id", stations()), In("variable", ["t2m", "rh2m"]), EqualTo("qc_flag", "ok")),
            selected_fields=("station_id", "obs_time", "variable", "value"),
        )
        .to_pandas()
    )
    out = pa.Table.from_pandas(build_features(obs), preserve_index=False).cast(NETWORK_FEATURES_SCHEMA.as_arrow())
    replace_partition("gold.upwind_features", out, AlwaysTrue())
    return out.num_rows


def columns(df: pd.DataFrame) -> list[str]:
    cols = sorted(c for c in df.columns if c.startswith("up_"))
    if not cols:
        raise ValueError("upwind models need the up_* features joined onto gold")
    return cols
