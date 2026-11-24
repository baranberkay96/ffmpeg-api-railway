# NOTICE: FFmpeg 9 Media Processing API template

This is a community template. It is not affiliated with or endorsed by the FFmpeg project or Railway. FFmpeg is a trademark of Fabrice Bellard, originator of the FFmpeg project.

## Components

| Component | Source | License | How it is used |
|---|---|---|---|
| FFmpeg 9.1 (`ffmpeg`, `ffprobe`) | static build from https://github.com/wader/static-ffmpeg, image `mwader/static-ffmpeg:9.1@sha256:dd09509d8c2152bfa601c8c20e3dd166bde906d37951b9f7e8bbd922267f5ac9` | FFmpeg: LGPL-2.1+/GPL-2.0+ (this build enables GPL components such as x264 and x265, so the binaries are GPL); build scripts: MIT | Binaries copied unmodified into the image at build time on Railway; source at https://ffmpeg.org/download.html and in the static-ffmpeg build recipe |
| Python 3.13 | `python:3.13-slim-trixie@sha256:6d68eb6310a906f26c7638eb6ea7557d4261662df3181af2f80f1f9a7e34c549` | PSF-2.0 (Debian packages under their own licenses) | Base image |
| FastAPI 0.165.0, Uvicorn 0.54.0, python-multipart 0.0.34 | PyPI | MIT / BSD-3-Clause / Apache-2.0 | Web framework |
| redis-py 8.2.0 | PyPI | MIT | Redis client |
| boto3 1.44.10 | PyPI | Apache-2.0 | S3 client for the Railway Bucket |
| Redis 8.2 | `redis:8.2.11@sha256:b0b01167ff47bc3868e56a967895a26be4230891a1f42a78ab61cdd227accc8d` (Railway's Redis service definition) | AGPL-3.0 / RSALv2 / SSPLv1 (Redis 8) | Unmodified image as the job queue |
| FFmpeg icon | https://upload.wikimedia.org/wikipedia/commons/7/76/FFmpeg_icon.svg | as published on Wikimedia Commons (FFmpeg logo, LGPL) | Marketplace icon, unmodified, refers to the project |

## Files written for this template

Everything under `services/ffmpeg/` (Dockerfile, `app/*.py`) was written for this template; no upstream file was copied. Offered under the MIT License.

## Upstream checked (2026-11-24)

- wader/static-ffmpeg README and Docker Hub tags (9.1 pushed 2026-11-20; binaries at `/ffmpeg` and `/ffprobe`).
- FFmpeg documentation for `-protocol_whitelist`, `-fs`, `-progress`, `-threads` and the palettegen/paletteuse GIF filters.
- Railway Buckets guide (virtual-hosted-style URLs, presigned URLs for private buckets).

## Market templates reviewed

`ffmpeg-rest-api` (63 active projects; crisog/ffmpeg-rest 1.2.0, Apache-2.0: Node/Hono + BullMQ + Redis, upload only, optional S3), `ffmpeg-lite-clone` (0 active; volume-based) and `n8n-w-ffmpeg` (74 active; n8n with FFmpeg on the worker, not an API). Read for ideas only; no code or text was copied.
