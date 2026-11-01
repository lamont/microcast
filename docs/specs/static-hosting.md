# Static site hosting · spec

*2026-10-26. Replaces D8's hosting (nginx on the home cluster) with an object
store behind a CDN, published by Dagster. D8's content and privacy rule stand.
Design agreed in conversation 2026-10-24 → 26; not yet built.*

As in `dagster-k8s.md`, this is the generic half. The hostname, bucket name,
AWS account, DNS zone and credentials belong to whoever deploys it and are
not in this repo.

## Why

`microcast site` already builds plain files (two HTML shells, CSS, JS and one
`data.json`) that render in the browser. Today they're only seen on a laptop
(`localhost:8000`). The site should be reachable from a phone before a ride,
refreshed hourly through the day, at about $0 a month, and should later be
able to show different private places and routes to different groups of
people without anything private ever being downloadable by the wrong person.

## Phases

| Phase | Who sees what | In this spec |
|---|---|---|
| **A. Host it** | Everything `microcast site` builds today (status, analytics, the bridge forecast once `live-forecast.md` lands), at the site's hostname. Already privacy-clean under D8. | Built in full. |
| **B. Public narrows** | The public site's forecasts are the Golden Gate Bridge wind and the Golden Gate Park ride, with backtest performance per model at those two highlighted. The full leaderboard and analytics stay public. | Data layout ready for it; the page change is a follow-up. |
| **C. Login** | Signed-in people see extra places and routes (the house, local routes) according to the groups they're in. | Sketched only; gets its own spec. |

Phase A must not paint B or C into a corner. That's why the data is split by
audience from the start (below), even while there's only one audience.

## Done means (phase A)

1. The site's hostname serves the built site over HTTPS from a private S3
   bucket through CloudFront.
2. A Dagster asset publishes hourly, 08:00–21:00 America/Los_Angeles, after
   the site build. A publish uploads data files every run and the page shells
   only when they've changed. Nothing about it counts as a "deploy".
3. A visitor sees new data within about two minutes of a publish, with no
   CloudFront invalidation needed.
4. `microcast publish` works the same from a laptop given the same env vars.
5. Nothing in this repo names the hostname, bucket, account, DNS zone or
   credentials.
6. The steady-state AWS bill is within the always-free tiers, apart from
   Route 53, which is already paid.

## Why S3 + CloudFront (decided)

- The DNS zone stays in Route 53 (the user won't move it). Cloudflare Pages
  could still serve the site through a CNAME. Cloudflare Access, its free
  login and its main draw for phase C, needs the zone on Cloudflare. *(Our
  understanding; recheck only if Cloudflare comes back up.)*
- Cloudflare Pages' free plan caps deploys per month. Hourly publishing would
  need a workaround. S3 PUTs have no such cap.
- Cost: CloudFront's always-free tier (1 TB out, 10M requests a month) dwarfs
  this site's traffic. S3 storage for a few MB is cents. ACM certificates are
  free. Phase C's Cognito and Lambda@Edge costs are also near zero at this
  scale.

## Bucket layout

```
index.html  analytics.html  site.css  site.js        page shells
data/public.json                                     everyone
data/groups/<group>.json                             phase C only (see "Must not happen")
```

`microcast site` writes this same layout locally. `site.js` fetches
`data/public.json` instead of `data.json`. In phase A, `public.json` holds what
`data.json` holds today.

**Audience in the registry.** Each place and route in `config/places.yaml` /
`places.local.yaml` gets a `visibility:` key, either `public` or a list of
group names. The existing `audience:` key means who travels the route, not
who may see it, so it isn't reused for this. The rules:

- A place or route with no `visibility:` key is private to group `owner`.
- Stations are public, as D8 already treats their ids and scores. The house
  sensors are private, as their ids are.
- Each group's file holds only what that group may see beyond public. The
  page merges `public.json` with the group files it's allowed to fetch.
- **Builder's call:** whether to split hourly data (forecasts) from daily
  data (backtest, analytics) into separate files to shrink the hourly upload
  (`public.json` is 3.2 MB today, much smaller compressed). Fine either way;
  the layout above doesn't depend on it.

In phase A nothing is marked public except what the site already shows, so
the built `public.json` matches today's `data.json`. A test asserts that a
place with no `visibility:` key never reaches `public.json`.

## Caching

Upload with `Cache-Control`:

| Files | Cache-Control |
|---|---|
| `data/**` | `max-age=60` |
| `*.html`, `site.css`, `site.js` | `max-age=300` |

CloudFront respects these headers. No invalidations, so no distribution id in
the publisher's config. Turn on CloudFront compression; the JSON compresses
well.

**Ordering:** a publish uploads data files first, then any changed shells. If
a shell change needs a new data shape, `data/public.json` carries a
`schema: <int>` and `site.js` shows "site is updating, reload in a minute"
when they don't match. *(My call; it keeps a five-minute cache from showing
a broken page.)*

## `microcast publish`

```
microcast publish [--from /data/site]
```

- Uses `boto3` (already pulled in by `dagster-aws`; add it to the `site` or
  `dagster` extra explicitly).
- Config from the environment: `MICROCAST_PUBLISH_BUCKET`, plus the standard
  AWS variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`,
  or `AWS_PROFILE` on a laptop). If `MICROCAST_PUBLISH_BUCKET` is unset, the
  command errors out.
- Uploads every file under `data/` each run, with content types set
  explicitly. Uploads a shell only when its MD5 differs from the object's
  ETag (single-part uploads, so the ETag is the MD5).
- Never deletes objects in phase A. *(My call: a stale extra file is
  harmless, and a wrong delete takes the site down.)*
- Prints what it uploaded and the total bytes.

## Dagster

A `site_publish` asset depending on `site`, pool none, size light. After
`live-forecast.md` lands, the hourly chain is forecast → `site` → `site_publish`.
Until then `site` and `site_publish` run hourly on the same 08:00–21:00 Pacific
schedule, rebuilding from the latest backtest and freshness. That's about 14
runs a day, ~430 a month.

`site_publish` gets `MICROCAST_PUBLISH_BUCKET` and the AWS credentials from a
Secret, alongside the others in `dagster-k8s.md`'s runtime contract. Add the
rows there.

## AWS resources (deployer's side)

Created in the deployer's own Terraform, not in this repo. Listed so a
deployer knows what to make:

| Resource | Notes |
|---|---|
| S3 bucket | Block all public access; no website hosting endpoint. |
| CloudFront distribution | Origin = the bucket via Origin Access Control; default root object `index.html`; HTTPS only (redirect); compression on; managed `CachingOptimized` policy (it honours origin `Cache-Control`); price class 100 (US/EU edges, cheapest). The site's hostname as an alternate domain name. |
| Bucket policy | Allows `s3:GetObject` to that distribution only. |
| ACM certificate | In **us-east-1** (CloudFront's requirement), DNS-validated in the zone. |
| Route 53 records | `A` and `AAAA` aliases for the hostname to the distribution, plus the certificate's validation CNAME. In the DNS zone's own Terraform. |
| IAM user for the publisher | Policy: `s3:PutObject` and `s3:GetObject` on `arn:aws:s3:::<bucket>/*`, `s3:ListBucket` on the bucket. Nothing else. Its access key goes in the cluster Secret. |

*My call:* no example Terraform in this repo. The table is the contract.
Unlike `deploy/k8s/`, a generic module here would be one more thing to keep
working that nobody runs from this repo.

## Phase C sketch (not built here)

- **Cognito** user pool with groups matching the `visibility:` group names.
  Hosted UI for sign-in, so we write no login page.
- **Lambda@Edge** on viewer-request for `data/groups/*`: verify the signed-in
  cookie's ID token and serve `data/groups/<g>.json` only if `<g>` is in its
  `cognito:groups` claim; otherwise 403. Everything else stays public and
  cached.
- `site.js` learns which groups it may fetch from the token and merges those
  files over `public.json`. A visitor who isn't signed in fetches only
  `public.json` and sees no trace of private places.
- Open for that spec: one site with a sign-in link vs. a separate private
  path; how the user adds people to groups; session length.

## Must not happen

- **Any file under `data/groups/` uploaded before phase C's edge check
  exists.** Until then the publisher refuses to upload that prefix, and
  `microcast site` writes group files only when asked
  (`--groups`, for local viewing).
- Home coordinates, sensor indexes or route geometry in any published file,
  public or group (D8). Group files may name private places and routes; the
  rule on coordinates still applies to them.
- The hostname, bucket, account id or credentials in this repo.
- A publish that leaves the site showing a broken page (see "Ordering").
- Hourly runs outside 08:00–21:00 Pacific.

## Checks

- Unit: the visibility split (a default-private place never reaches
  `public.json`; group files contain only their group's extras); the
  publisher's change detection and Cache-Control mapping, using a stubbed S3
  client (moto or botocore's Stubber).
- A real publish from the laptop to the deployed bucket, then the site loads
  on a phone off the home network, and a second publish shows new
  `generated_at` within two minutes.
- `curl -I` on `data/public.json` shows `cache-control: max-age=60` and a
  compressed response.

## Order of work

1. `visibility:` in the registry, and `microcast site` writing
   `data/public.json` (with `schema`). Point `site.js` at it.
2. `microcast publish`, with tests.
3. The deployer creates the AWS resources; one publish from the laptop.
4. The `site_publish` asset and its schedule; the Secret on the cluster.
5. Docs: a new decision (D14, "Site hosted on S3 + CloudFront, published by
   Dagster") superseding D8's hosting paragraph; the runtime contract rows
   in `dagster-k8s.md`; README's working vs planned; phase-1 Next #10.

## Settled (the user's, 2026-10-26)

- Phase A goes up public but unlisted, with the full site and no access
  gate; D8 already made the content public-safe.
- In phase B the full leaderboard and analytics across all stations stay
  public. Only private places and routes move behind the login.
