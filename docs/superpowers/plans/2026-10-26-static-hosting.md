# Static Site Hosting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Phase A of the hosting spec. `microcast site` writes an audience-split data layout, and `microcast publish` pushes it to an S3 bucket served by CloudFront.

**Architecture:** Places and routes gain a `visibility:` key in the registry. `site.build` splits its one data dict into `data/public.json` (always) and `data/groups/<group>.json` (only with `--groups`). A new `microcast.publish` module uploads to S3 with boto3: data every run, shells only when their MD5 changed, `data/groups/` refused. The Dagster asset itself waits for `microcast.orchestrate`, which doesn't exist yet, so this plan adds it to `dagster-k8s.md`'s asset table instead.

**Tech Stack:** Python 3.12, typer, boto3/botocore (new `publish` extra), pytest; vanilla JS in `src/microcast/site/static/site.js`.

**Spec:** `docs/specs/static-hosting.md`

## Global Constraints

- Nothing in the repo names the bucket, AWS account, DNS zone, the deployer's Terraform repos, or credentials. The site's hostname may appear.
- The working tree carries unrelated uncommitted work (MADIS/Synoptic: `registry.py`, `places.yaml`, `cli.py`, docs). It must be committed by its owner before Task 1. Each task stages only its own files, by path.
- Cache-Control: `data/**` → `max-age=60`; `*.html`, `site.css`, `site.js` → `max-age=300`.
- No CloudFront invalidations; the publisher needs no distribution id.
- The publisher never deletes objects and never uploads anything under `data/groups/`.
- Default visibility for a place or route with no `visibility:` key is the group `owner`. Registry stations and unregistered (MADIS) stations are public.
- Published files never contain coordinates, the PurpleAir index, or route geometry (D8).
- Data schema version: `SCHEMA = 1`, shared by `site/__init__.py` and `site.js`.
- Hourly publishing runs 08:00–21:00 America/Los_Angeles.
- Run tests with `uv run pytest`; lint with `uv run ruff check . && uv run ruff format --check .`.

## Review Focus

1. **A private place's sensor id in the obs and score tables** (`purpleair_home`, a future `station_home`) is a station id but belongs to the private `home` place. It must be withheld from `public.json`. → Task 2, `test_private_place_sensor_rows_withheld`.
2. **Route point ids are `<route>:<seq>`.** A score row for `school_walk:3` must follow `school_walk`'s visibility. → Task 2, `test_route_point_ids_follow_route_visibility`.
3. **Group names become S3 keys and filenames**, so `visibility: [Kids Ride]` or `../x` must fail at registry load, not produce odd keys. → Task 1, `test_bad_group_name_is_a_clear_error`.
4. **`visibility: [public, friends]`** is ambiguous and must be rejected. → Task 1, `test_public_mixed_with_groups_is_rejected`.
5. **A file in the site directory the publisher doesn't know** (a stray `.DS_Store`, a new `.png`) must not be uploaded with a guessed content type. The publisher skips dotfiles and errors on unknown extensions. → Task 3, `test_unknown_extension_is_an_error` and `test_dotfiles_skipped`.

---

### Task 1: `visibility:` in the registry

**Files:**
- Modify: `src/microcast/registry.py` (`Place`, `Route`, `load`)
- Modify: `config/places.yaml` (`gg_park_ride` gets `visibility: public`; `home` and `school_walk` keep the default; add one comment line explaining the key)
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `Place.visibility: list[str]` and `Route.visibility: list[str]`, default `["owner"]`; `["public"]` means public. `Registry.visibility() -> dict[str, list[str]]` maps every place, route and place-sensor id (each id in a place's `sensors:` list) to its target's visibility. Stations don't appear.

- [ ] **Step 1: Write the failing tests**

```python
def test_visibility_defaults_and_public():
    reg = registry.load(settings.REPO_ROOT / "config" / "places.yaml")
    vis = reg.visibility()
    assert vis["home"] == ["owner"]
    assert vis["purpleair_home"] == ["owner"]  # a place's sensor follows its place
    assert vis["school_walk"] == ["owner"]
    assert vis["gg_park_ride"] == ["public"]
    assert "GGBC1" not in vis

def test_visibility_string_or_list(tmp_path): ...
    # `visibility: public` → ["public"]; `visibility: [family, friends]` → ["family", "friends"]

def test_public_mixed_with_groups_is_rejected(tmp_path):
    # visibility: [public, friends] → ValueError matching "public"

def test_bad_group_name_is_a_clear_error(tmp_path):
    # visibility: ["Kids Ride"] and ["../x"] → ValueError matching "group name"
```

Write the two `tmp_path` tests by writing a minimal YAML (one place, `lat: 1`, `lon: 2`, no routes) into `tmp_path / "config" / "places.yaml"`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/test_registry.py -k visibility -v`
Expected: FAIL, `AttributeError: 'Registry' object has no attribute 'visibility'`

- [ ] **Step 3: Implement**

Add `_visibility(value: Any, owner: str) -> list[str]` to `registry.py`. `None` → `["owner"]`; a string → a one-item list. Group names must match `^[a-z0-9][a-z0-9_-]*$`; otherwise raise `ValueError` naming the target and "group name". If `"public"` appears alongside anything else, raise `ValueError`. Add `visibility` as the last dataclass field on both `Place` and `Route`, and add `Registry.visibility()`.

- [ ] **Step 4: Run the registry tests**

Run: `uv run pytest tests/test_registry.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/microcast/registry.py config/places.yaml tests/test_registry.py
git commit -m "Registry: visibility per place and route, default owner"
```

### Task 2: Site writes `data/public.json`, groups on request

**Files:**
- Modify: `src/microcast/site/__init__.py` (`SCHEMA`, `split`, `build`, module docstring)
- Modify: `src/microcast/site/static/site.js:573-583` (fetch path, schema check)
- Modify: `src/microcast/cli.py` (`site_cmd` gains `--groups`)
- Modify: `README.md:166-167` (the local serve lines still work; mention `--groups`)
- Test: `tests/test_site.py`

**Interfaces:**
- Consumes: `Registry.visibility()` from Task 1.
- Produces: `site.SCHEMA = 1`. `site.split(data: dict, vis: dict[str, list[str]]) -> tuple[dict, dict[str, dict]]` returns `(public, {group: withheld})`. `site.build(out_dir: Path, groups: bool = False) -> Path` writes `out_dir/data/public.json`, plus `out_dir/data/groups/<g>.json` only when `groups`. It no longer writes `data.json`.

**What `split` does.** A row is private when its id, cut at the first `:`, is a key in `vis` whose value isn't `["public"]`. The id-bearing rows are `leaderboard[].point`, `analytics.raw_bias[].point_id`, `stations[].id` and `freshness.stations[].station_id`. Private rows leave `public`. Each private row goes into every group in its visibility list, so a group file has the same nesting (`{"leaderboard": [...], "analytics": {"raw_bias": [...]}, ...}`) but holds only its rows. Pooled rows (`point` = `all` / `original`, `monthly`, `by_lead`, `by_hour`, `pit`) stay public, per D8's aggregate-scores rule. `public` gains `schema: SCHEMA`, and each group file carries `schema` and `generated_at` too.

- [ ] **Step 1: Update existing tests, then write the failing tests**

Existing tests read `out / "data.json"`. Change them to `out / "data" / "public.json"`, and give `_Registry` a `visibility()` that returns `{}`. New tests call `split` directly with hand-built dicts:

```python
def test_split_keeps_public_and_withholds_private():
    data = {"leaderboard": [{"point": "all"}, {"point": "KSFO"}, {"point": "home"}],
            "stations": [{"id": "KSFO"}, {"id": "home"}], "analytics": {"raw_bias": []},
            "freshness": {"stations": []}, "generated_at": "t"}
    public, groups = site.split(data, {"home": ["owner"]})
    assert [r["point"] for r in public["leaderboard"]] == ["all", "KSFO"]
    assert public["schema"] == site.SCHEMA
    assert groups["owner"]["leaderboard"] == [{"point": "home"}]

def test_private_place_sensor_rows_withheld():
    # vis {"purpleair_home": ["owner"]}: a freshness.stations row with station_id purpleair_home
    # is absent from public, present in groups["owner"]

def test_route_point_ids_follow_route_visibility():
    # vis {"school_walk": ["family"], "gg_park_ride": ["public"]}:
    # raw_bias rows for school_walk:3 go to groups["family"]; gg_park_ride:0 stays public

def test_build_writes_groups_only_when_asked(monkeypatch, tmp_path, catalog):
    # _Registry.visibility() returns {"home": ["owner"]}; build(groups=False)
    # → no out/data/groups dir; build(groups=True) → out/data/groups/owner.json exists
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/test_site.py -v`
Expected: FAIL (`split` is missing; `data/public.json` doesn't exist yet)

- [ ] **Step 3: Implement `split` and `build(out_dir, groups=False)`**

`build` calls `reg.visibility()` once and passes the result to `split`. It writes JSON as it does today (`default=str`). Update the module docstring: data now lives in `data/public.json`, group files are opt-in, and publishing is `microcast publish`.

- [ ] **Step 4: Point `site.js` at the new file**

Add `const SCHEMA = 1;`, fetch `data/public.json`, and before rendering:
if `data.schema !== SCHEMA`, add `el("p", { class: "sub", text: "The site is updating; reload in a minute." }, main)` and stop. Update the catch message to name `data/public.json`.

- [ ] **Step 5: `microcast site --groups`**

`site_cmd(out: str = ..., groups: bool = typer.Option(False, help="Also write data/groups/<group>.json, for viewing locally; never published"))` calls `build(Path(out), groups=groups)`.

- [ ] **Step 6: Run the tests and lint**

Run: `uv run pytest tests/test_site.py tests/test_registry.py -v && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS, lint clean

- [ ] **Step 7: Check it by eye**

Run: `uv run microcast site --out site && python -m http.server -d site 8000`, then open both pages.
Expected: both pages render as before, and the footer's "Generated" time is now.

- [ ] **Step 8: Commit**

```bash
git add src/microcast/site/__init__.py src/microcast/site/static/site.js src/microcast/cli.py README.md tests/test_site.py
git commit -m "Site: data/public.json with schema; group files only on --groups"
```

### Task 3: `microcast publish`

**Files:**
- Create: `src/microcast/publish.py`
- Modify: `pyproject.toml` (new extra `publish = ["boto3>=1.40"]`; then `uv lock`)
- Modify: `src/microcast/cli.py` (new `publish` command)
- Modify: `.env.example` (commented `# MICROCAST_PUBLISH_BUCKET=` with a one-line note; no real name)
- Test: `tests/test_publish.py`

**Interfaces:**
- Consumes: the layout from Task 2 (`data/public.json`, shells at the root).
- Produces: `publish.publish(site_dir: Path, bucket: str, client=None, log=print) -> list[str]` returns the uploaded keys in upload order. `client` defaults to `boto3.client("s3")`. `publish.CACHE_DATA = "max-age=60"`, `publish.CACHE_SHELL = "max-age=300"`. `publish.CONTENT_TYPES: dict[str, str]` maps `.html` → `text/html; charset=utf-8`, `.css` → `text/css; charset=utf-8`, `.js` → `text/javascript; charset=utf-8`, `.json` → `application/json`.

**Rules:** walk `site_dir` and skip dotfiles. Any path under `data/groups/` raises `ValueError` before anything is uploaded. An extension not in `CONTENT_TYPES` raises `ValueError` before anything is uploaded. Every `data/**` file is uploaded. A shell is uploaded only when `head_object` returns 404, or its `ETag.strip('"')` differs from the local MD5 hex. Data files go first, then shells. `log` prints each key and, at the end, the total bytes.

- [ ] **Step 1: Write the failing tests**

Use a small fake client class in the test that records `put_object` kwargs and answers `head_object` from a dict. A missing key raises `botocore.exceptions.ClientError({"Error": {"Code": "404"}}, "HeadObject")`.

```python
def test_first_publish_uploads_all_data_first(tmp_path): ...
    # site with index.html, site.js, data/public.json; empty bucket
    # → keys == ["data/public.json", "index.html", "site.js"] (data first, shells sorted)
    # → put for data/public.json has CacheControl "max-age=60", ContentType "application/json"
    # → put for index.html has CacheControl "max-age=300", ContentType "text/html; charset=utf-8"

def test_unchanged_shells_skipped_data_always_uploaded(tmp_path): ...
    # bucket ETags equal the local MD5s → keys == ["data/public.json"]

def test_group_files_refused(tmp_path): ...
    # data/groups/owner.json present → ValueError matching "data/groups", no put_object calls

def test_unknown_extension_is_an_error(tmp_path): ...
    # logo.png present → ValueError matching ".png", no put_object calls

def test_dotfiles_skipped(tmp_path): ...
    # .DS_Store present → not uploaded, no error
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/test_publish.py -v`
Expected: FAIL, `ModuleNotFoundError: microcast.publish` (after `uv lock && uv sync --all-extras` for boto3)

- [ ] **Step 3: Implement `publish.py`**

Import `boto3` inside the function, as the CLI imports heavy modules lazily. A non-404 `ClientError` from `head_object` re-raises.

- [ ] **Step 4: Add the CLI command**

`@app.command("publish") def publish_cmd(src: str = typer.Option("site", "--from", help="Built site directory"))`. It reads `MICROCAST_PUBLISH_BUCKET` from the environment. If that's unset, it raises `typer.BadParameter("set MICROCAST_PUBLISH_BUCKET")`. Then it calls `publish.publish(Path(src), bucket, log=typer.echo)`.

- [ ] **Step 5: Run all tests and lint**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS, lint clean

- [ ] **Step 6: Commit**

```bash
git add src/microcast/publish.py src/microcast/cli.py pyproject.toml uv.lock .env.example tests/test_publish.py
git commit -m "microcast publish: upload the site to S3 (data every run, shells when changed)"
```

### Task 4: Docs and the Dagster contract

**Files:**
- Modify: `docs/decisions.md` (new `## D14 · Site hosted on S3 + CloudFront, published by Dagster` after D13; in D8's **Hosting** paragraph, add one line: "Superseded by D14 (2026-10-26).")
- Modify: `docs/specs/dagster-k8s.md` (asset table: a `site_publish` row; runtime contract: a publish row)
- Modify: `docs/specs/phase-1.md:234` (Next #10 now points at `static-hosting.md`)
- Modify: `README.md` ("Planned, not built": hosting now cites D14 and `static-hosting.md`)

**Interfaces:**
- Consumes: the command and env var names from Task 3.

- [ ] **Step 1: Write D14**

Write it in the style of D8–D13: date, **Decision**, **Why**, **Changes**. The content comes from the spec's "Why S3 + CloudFront" and "Phases" sections, and names no bucket, account or Terraform repo.

- [ ] **Step 2: Add the `dagster-k8s.md` rows**

Asset table: `| site_publish | publish --from /data/site | none | after site; hourly 08:00–21:00 America/Los_Angeles | none | light |`. Runtime contract: `| Site publishing | MICROCAST_PUBLISH_BUCKET, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION from a Secret | Write-only to the site bucket; see static-hosting.md. |`. Also add a sentence that `site` runs on the same hourly daytime schedule until `live-forecast.md` chains forecast → site → publish.

- [ ] **Step 3: Phase-1 Next #10 and the README**

- [ ] **Step 4: Commit**

```bash
git add docs/decisions.md docs/specs/dagster-k8s.md docs/specs/phase-1.md README.md
git commit -m "D14: site on S3 + CloudFront, published hourly by Dagster"
```

### Task 5: First real publish (deployer and user)

Nothing in the repo changes. These are the spec's manual checks, run once the deployer has created the resources from the spec's "AWS resources" table.

- [ ] **Step 1: Publish from the laptop**

Run: `MICROCAST_PUBLISH_BUCKET=<bucket> AWS_PROFILE=<profile> uv run microcast publish --from site`
Expected: four shells and `data/public.json` uploaded; a second run uploads only `data/public.json`.

- [ ] **Step 2: Check headers**

Run: `curl -sI -H 'Accept-Encoding: gzip, br' https://<host>/data/public.json`
Expected: `cache-control: max-age=60` and a `content-encoding` header.

- [ ] **Step 3: Check freshness**

Rebuild, publish, and reload on a phone off the home network.
Expected: the footer's "Generated" time updates within two minutes.
