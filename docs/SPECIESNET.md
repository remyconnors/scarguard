# SpeciesNet species classification

ScarGuard's detector identifies *what kind of thing* is in front of the
camera (`bird`, `raccoon`, `duck`) but a generic COCO-trained YOLO can't
tell a Great Blue Heron from an American Crow. The optional
`speciesnet` sidecar closes that gap by forwarding every bird-class
detection to an external SpeciesNet classifier and writing the result
back to the dashboard.

## Architecture

```
detector ──pub─▶ scarguard:detections ──sub─▶ speciesnet sidecar
                                                 │
                                                 ├─ crop bbox from snapshot
                                                 ├─ POST → SpeciesNet API
                                                 ├─ poll until classified
                                                 └─ write /data/speciesnet.db
                                                       │
                                                       ▼
                                          web service reads on the events page
                                          + scarguard:species SSE patch
```

The classifier itself is **out of scope for ScarGuard**. The reference
deployment is the AWS Lambda described in
`speciesnet-lambda/README.md` (separate project) — a containerised
SpeciesNet 4.0.2a model behind API Gateway with USA geofencing and
async S3-triggered inference. Any HTTP API that exposes the same two
endpoints will work:

* `GET /upload-url?filename=<name>.jpg` → `{upload_url, image_id}`
* `PUT <upload_url>` (Content-Type: image/jpeg) → 200
* `GET /result?image_id=<id>` → 200 with `{predictions: [...]}` once
  classification finishes, 202 `{status: "processing"}` while still
  running.

## Enabling

1. Stand up a SpeciesNet HTTP API (the AWS Lambda recipe takes ~10
   minutes if you have an AWS account).
2. Add a `speciesnet:` block to `scarguard.yml`:

   ```yaml
   speciesnet:
     enabled: true
     api_url: "https://abc123.execute-api.us-east-1.amazonaws.com"
     # Optional Bearer token if you put API Gateway behind auth:
     # api_token: "..."
     trigger_classes: ["bird"]   # only classify these YOLO classes
     min_confidence: 0.40        # skip low-confidence detections
     timeout_seconds: 15         # per-HTTP-call timeout
     poll_interval_seconds: 3    # gap between /result polls
     max_polls: 60               # ≈ 3 minutes total before giving up
     bbox_padding_pct: 0.10      # 10% margin around bbox crop
     max_concurrent: 4           # worker pool size
   ```

3. `docker compose up speciesnet` (or the whole stack). The sidecar
   logs `speciesnet enabled (api=...)` once it picks up the config.

## What appears on the events page

Each row gains a **Species** column, populated when the classification
lands:

* While the sidecar is still working: *classifying…*
* On success: `American Crow (87%)`. Hover for the binomial name.
* When SpeciesNet's geofence rolled the prediction up to genus/family
  because the species isn't allowed in the configured country (the
  Lambda defaults to USA), the label is shown in amber with a tooltip
  noting the rollup.
* On error or timeout: a muted `error`/`timeout` chip with the failure
  message in the title attribute.

The cell updates live via SSE — no page reload needed when a
classification completes.

## Data model

The sidecar owns a separate SQLite database
(`/data/speciesnet.db`) — keeping it out of `scarguard.db` preserves
the project's "one writer per database" rule. The web service mounts
the file read-only and joins on `feedback_token` (already unique on
every detection event). Schema:

```sql
CREATE TABLE species_classifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_token  TEXT UNIQUE NOT NULL,
    camera_name     TEXT NOT NULL,
    detection_class TEXT NOT NULL,
    status          TEXT NOT NULL,  -- pending | success | timeout | error
    common_name     TEXT,
    species         TEXT,
    genus           TEXT,
    family          TEXT,
    "order"         TEXT,
    class           TEXT,
    score           REAL,
    geofenced       INTEGER,
    top_predictions TEXT,            -- JSON array, top-5 raw response
    error           TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);
```

## Operational notes

* **Cold starts.** Container Lambdas with the cropped 200 MB SpeciesNet
  model take 60-180s to warm up after idle. Default `max_polls=60` ×
  `poll_interval=3s` covers that with margin. If you keep your Lambda
  warm via scheduled pings, drop `poll_interval_seconds` to 1.
* **HMAC signing.** The sidecar verifies `DETECTION_HMAC_KEY` on
  incoming Redis events the same way the notifier and deterrent do —
  spoofed events get rejected.
* **SSRF defence.** The configured `api_url` and the presigned upload
  URL returned by `/upload-url` are both validated against
  `shared/url_safety.py` before any HTTP traffic flows. A
  misconfigured-or-malicious URL pointing at `127.0.0.1` or a private
  range is refused at request time.
* **Cost control.** Set `min_confidence` and `trigger_classes` to keep
  the per-event Lambda spend predictable. Default config classifies
  only the COCO `bird` class above 40% confidence.
* **Disabling.** `enabled: false` (or omitting the section entirely)
  short-circuits the sidecar without restarting it; the events page
  silently stops gaining species labels for new detections.
