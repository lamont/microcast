"""RRFS collection: the byte-range search keeps only the fields and the top-of-hour step we use."""

from __future__ import annotations

import re

from microcast.ingest import rrfs

# Lines as they appear in a 2dfld.3km.subh .idx (f003): four 15-minute steps per field.
IDX = """\
12:12166093:d=2026101707:TMP:2 m above ground:135 min fcst:
86:83229773:d=2026101707:TMP:2 m above ground:165 min fcst:
123:119029630:d=2026101707:TMP:2 m above ground:3 hour fcst:
125:122249608:d=2026101707:DPT:2 m above ground:3 hour fcst:
126:124041214:d=2026101707:UGRD:10 m above ground:3 hour fcst:
127:125958489:d=2026101707:VGRD:10 m above ground:3 hour fcst:
115:110786850:d=2026101707:GUST:surface:3 hour fcst:
112:107860285:d=2026101707:VIS:surface:3 hour fcst:
137:130436011:d=2026101707:HGT:cloud ceiling:3 hour fcst:
136:128663306:d=2026101707:HGT:cloud base:3 hour fcst:
140:133000000:d=2026101707:DSWRF:surface:3 hour fcst:
141:133000219:d=2026101707:UGRD:80 m above ground:3 hour fcst:
142:133100000:d=2026101707:APCP:surface:0-3 hour acc fcst:
"""


def test_search_takes_top_of_hour_fields_only():
    hits = [line.split(":", 3)[3] for line in IDX.splitlines() if re.search(rrfs.SEARCH, line)]
    assert hits == [
        "TMP:2 m above ground:3 hour fcst:",
        "DPT:2 m above ground:3 hour fcst:",
        "UGRD:10 m above ground:3 hour fcst:",
        "VGRD:10 m above ground:3 hour fcst:",
        "GUST:surface:3 hour fcst:",
        "VIS:surface:3 hour fcst:",
        "HGT:cloud ceiling:3 hour fcst:",
        "DSWRF:surface:3 hour fcst:",
    ]


def test_batch_id():
    from datetime import UTC, datetime

    assert rrfs.batch_id(datetime(2026, 10, 17, 7, tzinfo=UTC)) == "rrfs/2026-10-17T07"
