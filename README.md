# Deploy and Host FFmpeg API with Railway

FFmpeg is the open-source toolkit behind most video and audio processing. This template turns FFmpeg 9.1 into a private HTTP API: send a file or a URL, pick an operation (convert, extract audio, thumbnail, frames, GIF, probe or your own FFmpeg arguments) and download the result. An API key is generated at deploy time.

> Community template, not affiliated with the FFmpeg project.

## About Hosting FFmpeg API

Running FFmpeg as a service needs more than the binary: uploads must be stored somewhere, long encodes must not block the web server, and one oversized request must not take the whole service down. This template splits the work into an API service and a worker service connected by a Redis queue. Uploads and results live in a Railway Bucket, so the API and the workers share files without a shared disk, and outputs are served by download or a short-lived direct link.

Every job has limits: input size (500 MB by default), output size (2 GB), wall-clock time (30 minutes) and queue length. FFmpeg runs as an unprivileged user without network protocols, and URL inputs are refused when they point at private addresses. Results and job records are deleted automatically after 24 hours.

## Common Use Cases

- Media backend for n8n, Make, Zapier or your own app: convert uploads to MP4/WebM, normalise audio to MP3
- Video thumbnails, poster frames, contact sheets (frames as ZIP) and short GIF previews
- Extract audio from videos for transcription or podcast workflows
- Inspect media (codec, duration, resolution, bitrate) before accepting an upload
- Batch transcoding with several workers behind one queue

## Dependencies for FFmpeg API Hosting

- FFmpeg API service (FastAPI, repo build with FFmpeg 9.1 static binaries)
- FFmpeg Worker service (same image, runs the jobs)
- Redis (job queue and job records, with a volume)
- Railway Bucket (uploads and outputs)

### Deployment Dependencies

- [FFmpeg documentation](https://ffmpeg.org/documentation.html)
- [wader/static-ffmpeg](https://github.com/wader/static-ffmpeg) (the static FFmpeg build used)
- [Railway Buckets](https://docs.railway.com/guides/storage-buckets)
- [Template source](https://github.com/baranberkay96/ffmpeg-api-railway)

### Implementation Details

| Service | Purpose |
|---|---|
| FFmpeg API | Public HTTPS API on port 8080: auth, uploads, queue, downloads, `/docs` (OpenAPI) |
| FFmpeg Worker | Takes jobs from the queue and runs FFmpeg; private `/healthz` only |
| Redis | Job queue and job records |
| Bucket | Uploaded inputs (deleted after the job) and outputs (deleted after `RETENTION_HOURS`) |

**First steps**

1. Copy `API_KEY` from the FFmpeg API service's Variables tab.
2. Convert a file and get the MP4 back in one call:

```bash
curl -H "Authorization: Bearer {API_KEY}" \
  -F operation=convert -F format=mp4 -F width=1280 \
  -F file=@input.mov -o output.mp4 \
  https://{your-domain}/v1/run
```

3. Or queue a job from a URL and poll it:

```bash
curl -H "Authorization: Bearer {API_KEY}" -H "Content-Type: application/json" \
  -d '{"operation":"thumbnail","url":"https://example.com/video.mp4","options":{"time":5,"width":640}}' \
  https://{your-domain}/v1/jobs
curl -H "Authorization: Bearer {API_KEY}" https://{your-domain}/v1/jobs/{job-id}
curl -H "Authorization: Bearer {API_KEY}" -o thumb.jpg https://{your-domain}/v1/jobs/{job-id}/output
```

A finished job also carries `output.url`, a direct Bucket link valid for `PRESIGNED_URL_SECONDS`. Add `callback_url` to a job to receive the finished job as a JSON POST. The interactive reference is at `https://{your-domain}/docs`.

**Operations**

| Operation | Main options |
|---|---|
| `convert` | `format` (mp4, webm, mkv, mov, avi, ts, mp3, m4a, aac, wav, flac, ogg, opus, jpg, png, webp), `video_codec` (h264, h265, vp9, av1, mpeg4, copy), `audio_codec`, `crf`, `preset`, `width`, `height`, `fps`, `video_bitrate`, `audio_bitrate`, `start`, `duration`, `no_audio` |
| `extract_audio` | `format` (mp3), `bitrate`, `audio_channels`, `sample_rate` |
| `thumbnail` | `time`, `width`, `height`, `format` |
| `frames` | `fps`, `max_frames`, `width`, `format` (ZIP of images) |
| `gif` | `start`, `duration` (max 60 s), `fps`, `width` |
| `probe` | none (returns ffprobe JSON; `/v1/probe` answers directly) |
| `custom` | `args` (your FFmpeg output arguments), `input_args`, `format` |

`custom` arguments are checked: no extra inputs or outputs, no URLs or file paths, no filters that read files. Set `ALLOW_CUSTOM_ARGS=false` to switch it off.

**Limits and variables** (FFmpeg API service; the worker reads the same values)

- `MAX_INPUT_MB` (500), `MAX_OUTPUT_MB` (2000), `JOB_TIMEOUT_SECONDS` (1800), `MAX_QUEUED_JOBS` (100), `RETENTION_HOURS` (24), `SYNC_WAIT_SECONDS` (300).
- Worker: `WORKER_CONCURRENCY` (1), `FFMPEG_THREADS` (empty = from the CPU limit, max 8), `ALLOW_PRIVATE_URLS` (false).
- An invalid value falls back to the default with a warning in the log. An API key shorter than 16 characters locks the API (401 for everyone) until it is fixed.

**Scaling**: raise the FFmpeg Worker's replicas (Settings, Deploy) to run more jobs in parallel; each replica takes one job at a time by default. Encoding is CPU-bound: give the worker more vCPU for faster jobs. Requests through Railway's public proxy are limited to about 15 minutes, so use `/v1/jobs` and polling or `callback_url` for long encodes.

**Resources** (measured on Railway): the API idles at about 80 MB RAM, a worker at about 55 MB and Redis at 30 MB. While encoding, FFmpeg uses the worker's vCPUs and memory depending on resolution and codec: a 1080p H.265 encode with 8 threads peaked at 1.8 GB. Trial plans (1 GB) suit audio, thumbnails and H.264 up to 1080p; set `FFMPEG_THREADS=2` there to keep memory low, and use Hobby or Pro for H.265, AV1 or 4K.

**Upgrading**: FFmpeg and Python are pinned by digest in `services/ffmpeg/Dockerfile` of the template repository; change the `FFMPEG_IMAGE` tag and redeploy both services.

**Fair use**: the API downloads plain file URLs only. It has no site extractors and is not meant for downloading from YouTube or other streaming sites, which Railway's Fair Use Policy forbids.

### Why Deploy FFmpeg API on Railway?

Railway runs the API, the workers, Redis and the Bucket as one project with private networking and generated credentials. You get an authenticated media API in a few minutes, pay only for the CPU your encodes use, and scale workers with a slider instead of managing servers.
