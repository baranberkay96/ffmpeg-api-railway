# FFmpeg 9 Media Processing API: Railway template spec

## Listing

| Field | Value |
|---|---|
| Name | **FFmpeg 9 Media Processing API** |
| Description | FFmpeg REST API: convert, thumbnails, audio; queue workers, Bucket output. (74 chars) |
| Category | Other |
| Icon | `https://upload.wikimedia.org/wikipedia/commons/7/76/FFmpeg_icon.svg` |
| Repo | `baranberkay96/ffmpeg-api-railway`, service root `/services/ffmpeg` (both app services) |

## Base market templates

| Code | Active | Notes |
|---|---|---|
| `ffmpeg-rest-api` | 63 | crisog/ffmpeg-rest standalone image + Redis; fixed endpoints, uploads read fully into memory, no URL input, no job time limit, `stateless` by default |
| `n8n-w-ffmpeg` | 74 | n8n queue mode with FFmpeg on the worker; not an API |
| `ffmpeg-lite-clone` | 0 | Image `ghcr.io/mc9max/ffmpeg-lite`, volume, no auth visible in the config |

### Improvements over market templates

- API key generated at deploy (Bearer or `X-API-Key`), fail-closed when the key is unusable.
- Upload **or** URL input (SSRF-guarded: public addresses only by default, redirects re-checked).
- Separate API and worker services over a Redis queue: long encodes never block the API; workers scale with replicas.
- Railway Bucket for inputs and outputs (no shared volume needed), direct presigned links, automatic deletion after `RETENTION_HOURS`.
- Per-job limits: input size (streamed check, 413 before the disk fills), output size (`-fs`), wall-clock timeout, queue length (429).
- FFmpeg runs as an unprivileged user with an empty environment and `-protocol_whitelist file,pipe`; playlists (HLS/DASH/concat) are refused; custom arguments are validated.
- Threads sized from the cgroup CPU quota (STANDARD 8.15); progress, cancel (DELETE), callbacks, OpenAPI docs.
- FFmpeg 9.1 static build pinned by digest.

## Pinned versions (checked 2026-11-24)

| Component | Pin |
|---|---|
| FFmpeg | `mwader/static-ffmpeg:9.1@sha256:dd09509d…` (pushed 2026-11-20) |
| Python | `python:3.13-slim-trixie@sha256:6d68eb63…` |
| Python packages | `requirements.txt` (fastapi 0.165.0, uvicorn 0.54.0, python-multipart 0.0.34, redis 8.2.0, boto3 1.44.10) |
| Redis | `redis:8.2.11@sha256:b0b01167…` |

## Services

| Service | Source | Start | Healthcheck | Domain | Volume |
|---|---|---|---|---|---|
| FFmpeg API | repo `/services/ffmpeg`, `ROLE=api` | `python -m app` | `/healthz` (Redis ping) | public, 8080 | none |
| FFmpeg Worker | repo `/services/ffmpeg`, `ROLE=worker` | `python -m app` | `/healthz` on private 8080 | none | none (temp files in /tmp) |
| Redis | `redis:8.2.11` | Railway's command | none | none | `/data` |
| Bucket | Railway Bucket | | | | |

Restart policy `ALWAYS` on API and worker (they wait for Redis). Worker replicas can be raised; the API is stateless too.

## Resource profile

Measured 2026-11-24: idle API 75-95 MB, worker 55 MB, Redis 30 MB. A 1080p60s H.265 (medium, 8 threads) encode peaked at 1.8 GB on the worker and finished in ~25 s; H.264/audio jobs stay well below 1 GB. Idle cost about $1.5/month RAM; encoding cost is CPU time ($20/vCPU-month). Minimum plan: Trial for audio and HD; Hobby for 4K/AV1 or several workers.

## Verification checklist

1. `/healthz` 200 on API; worker deploy SUCCESS.
2. `/v1/operations` without key → 401; with key → 200.
3. Upload convert to MP4, thumbnail, extract_audio, frames, gif, probe; URL input; download via API and via presigned URL.
4. Timeout and size limits; DELETE a job; maintenance deletes old outputs.
5. Short `API_KEY` → service starts, API locked, log explains.

## Open questions

- Railway's public proxy cuts requests after ~15 minutes; very long encodes must use `/v1/jobs` + polling/callback (documented).
- Presigned URL host comes from the Bucket endpoint (virtual-hosted style) and was verified in the deploy test.
