"""HTTP API: authenticates clients, stores uploads in the Bucket, queues jobs in Redis."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import uuid

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from . import ops, store
from .config import Settings

log = logging.getLogger("ffmpeg.api")
MB = 1024 * 1024


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    """ASGI middleware: reject request bodies above MAX_INPUT_MB before they fill the disk."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length", b"")
        if length.isdigit() and int(length) > self.limit:
            return await _too_large(send, self.limit)
        seen = 0
        started = False

        async def counted_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except BodyTooLarge:
            if not started:
                await _too_large(send, self.limit)


async def _too_large(send, limit: int) -> None:
    body = json.dumps({"detail": f"request body is larger than MAX_INPUT_MB ({limit // MB - 1} MB)"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await check_dependencies()
        yield

    app = FastAPI(lifespan=lifespan, title="FFmpeg API", version="1.0.0",
                  description="Send a file or URL, pick an operation, download the result. "
                              "Authenticate with `Authorization: Bearer <API_KEY>` or `X-API-Key`.")
    app.add_middleware(BodyLimit, limit=settings.max_input_bytes + MB)
    r = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30, socket_connect_timeout=10,
                          health_check_interval=30)
    s3 = store.s3_client(settings) if settings.s3_configured else None

    def authorize(request: Request) -> None:
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
        if not supplied or not hmac.compare_digest(supplied.encode(), settings.api_key.encode()):
            raise HTTPException(401, "missing or wrong API key", headers={"WWW-Authenticate": "Bearer"})

    def presign(job: dict) -> str | None:
        if not (s3 and job.get("status") == "done" and job.get("output_key")):
            return None
        return s3.generate_presigned_url("get_object", ExpiresIn=settings.presign_seconds, Params={
            "Bucket": settings.s3_bucket, "Key": job["output_key"],
            "ResponseContentDisposition": f'attachment; filename="{job.get("output_name", "output")}"'})

    def view(job: dict) -> dict:
        return store.public_view(job, presign(job), settings.presign_seconds)

    async def load(job_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(404, "job not found")
        job = store.decode(await r.hgetall(store.job_key(job_id)))
        if not job:
            raise HTTPException(404, "job not found (unknown or older than RETENTION_HOURS)")
        return job

    async def wait_for(job_id: str, seconds: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            job = store.decode(await r.hgetall(store.job_key(job_id)))
            if not job or job.get("status") in store.FINAL or loop.time() >= deadline:
                return job
            await asyncio.sleep(0.5)

    async def parse(request: Request) -> tuple[dict, UploadFile | None]:
        ctype = request.headers.get("content-type", "")
        upload = None
        if ctype.startswith("multipart/form-data") or ctype.startswith("application/x-www-form-urlencoded"):
            form = await request.form(max_files=1, max_fields=50)
            fields = {}
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if key == "file":
                        upload = value
                else:
                    fields[key] = value
        elif ctype.startswith("application/json"):
            try:
                fields = await request.json()
            except ValueError:
                raise HTTPException(400, "body is not valid JSON") from None
            if not isinstance(fields, dict):
                raise HTTPException(400, "JSON body must be an object")
        else:
            raise HTTPException(415, "send multipart/form-data (with a file) or application/json (with a url)")
        options = fields.get("options") or {}
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except ValueError:
                raise HTTPException(400, "options must be a JSON object") from None
        if not isinstance(options, dict):
            raise HTTPException(400, "options must be a JSON object")
        # Top-level fields other than the reserved ones are options too (handy in form posts).
        merged = {**{k: v for k, v in fields.items() if k not in ops.RESERVED}, **options}
        for key, value in query_params(request).items():
            if key not in ops.RESERVED:
                merged.setdefault(key, value)
        fields.setdefault("operation", request.query_params.get("operation", ""))
        return {**fields, "options": merged}, upload

    def query_params(request: Request) -> dict:
        return {k: v for k, v in request.query_params.items() if k not in ("wait", "redirect")}

    async def create(request: Request, default_wait: int, operation: str | None = None) -> dict:
        authorize(request)
        fields, upload = await parse(request)
        if operation:
            fields["operation"] = operation
        try:
            options = ops.validate(fields.get("operation", ""), fields["options"], settings.allow_custom_args)
        except ops.OptionError as err:
            raise HTTPException(400, str(err)) from None
        if fields.get("filename"):
            options["filename"] = str(fields["filename"])[:120]
        url = str(fields.get("url") or "").strip()
        if bool(url) == bool(upload):
            raise HTTPException(400, "send exactly one input: a file (multipart field 'file') or a 'url'")
        if url and not re.match(r"^https?://", url, re.I):
            raise HTTPException(400, "url must start with http:// or https://")
        callback = str(fields.get("callback_url") or "").strip()
        if callback and not re.match(r"^https?://", callback, re.I):
            raise HTTPException(400, "callback_url must start with http:// or https://")
        if upload and not s3:
            raise HTTPException(503, "file uploads need the Railway Bucket (S3_* variables are empty)")
        if not s3 and options["operation"] != "probe":
            raise HTTPException(503, "outputs need the Railway Bucket (S3_* variables are empty)")
        if await r.llen(store.QUEUE) >= settings.max_queued:
            raise HTTPException(429, f"queue is full ({settings.max_queued} jobs); retry later",
                                headers={"Retry-After": "30"})
        job_id = uuid.uuid4().hex
        record = {"id": job_id, "status": "queued", "operation": options["operation"],
                  "options": json.dumps(options), "created_at": store.now(), "progress": 0,
                  "source": "upload" if upload else "url"}
        if upload:
            record["input_name"] = re.sub(r"[^A-Za-z0-9._ -]+", "_", upload.filename or "upload")[:200]
            await run_in_threadpool(s3.upload_fileobj, upload.file, settings.s3_bucket, f"jobs/{job_id}/input")
            await upload.close()
        else:
            record["input_url"] = url
        if callback:
            record["callback_url"] = callback
        key = store.job_key(job_id)
        async with r.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping=record)
            pipe.expire(key, settings.retention_hours * 3600)
            pipe.rpush(store.QUEUE, job_id)
            await pipe.execute()
        wait = fields.get("wait", request.query_params.get("wait", default_wait))
        try:
            wait = max(0.0, min(float(wait or 0), float(settings.sync_wait)))
        except ValueError:
            wait = 0.0
        return (await wait_for(job_id, wait)) if wait else store.decode(record)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        try:
            await r.ping()
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "redis unavailable"}, 503)
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "FFmpeg API", "docs": "/docs", "operations": list(ops.OPERATIONS),
                "auth": "Authorization: Bearer <API_KEY>"}

    @app.get("/v1/operations")
    async def operations(request: Request):
        authorize(request)
        return {"operations": ops.OPERATIONS, "limits": {
            "max_input_mb": settings.max_input_bytes // MB, "max_output_mb": settings.max_output_bytes // MB,
            "job_timeout_seconds": settings.job_timeout, "sync_wait_seconds": settings.sync_wait,
            "max_queued_jobs": settings.max_queued, "retention_hours": settings.retention_hours,
            "custom_args": settings.allow_custom_args}}

    @app.post("/v1/jobs", status_code=202)
    async def create_job(request: Request):
        """Queue a job. Returns at once, or waits up to `wait` seconds for the result."""
        job = await create(request, 0)
        return JSONResponse(view(job), status_code=200 if job.get("status") in store.FINAL else 202,
                            headers={"Location": f"/v1/jobs/{job['id']}"})

    @app.post("/v1/run")
    async def run(request: Request):
        """Queue a job, wait for it (SYNC_WAIT_SECONDS) and return the output file itself."""
        job = await create(request, settings.sync_wait)
        status = job.get("status")
        if status == "done":
            if job.get("operation") == "probe":
                return JSONResponse(job.get("result"))
            return await stream(job)
        if status in ("failed", "cancelled"):
            return JSONResponse(view(job), status_code=422)
        return JSONResponse(view(job), status_code=202, headers={"Location": f"/v1/jobs/{job['id']}"})

    @app.post("/v1/probe")
    async def probe(request: Request):
        """Media metadata (ffprobe JSON) for an uploaded file or URL."""
        job = await create(request, settings.sync_wait, operation="probe")
        if job.get("status") == "done":
            return JSONResponse(job.get("result"))
        code = 422 if job.get("status") in store.FINAL else 202
        return JSONResponse(view(job), status_code=code)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request, wait: float = 0):
        authorize(request)
        job = await load(job_id)
        if wait:
            job = await wait_for(job_id, max(0.0, min(wait, float(settings.sync_wait)))) or job
        return view(job)

    async def stream(job: dict):
        obj = await run_in_threadpool(s3.get_object, Bucket=settings.s3_bucket, Key=job["output_key"])
        body = obj["Body"]

        def chunks():
            try:
                yield from body.iter_chunks(chunk_size=MB)
            finally:
                body.close()

        name = job.get("output_name") or "output"
        return StreamingResponse(chunks(), media_type=job.get("content_type") or "application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"',
                                          "Content-Length": str(obj.get("ContentLength", "")),
                                          "X-Job-Id": job["id"]})

    @app.get("/v1/jobs/{job_id}/output")
    async def output(job_id: str, request: Request, redirect: bool = False):
        authorize(request)
        job = await load(job_id)
        if job.get("status") != "done" or not job.get("output_key"):
            raise HTTPException(409, f"job is {job.get('status')}; no output to download")
        if redirect:
            return RedirectResponse(presign(job), status_code=302)
        return await stream(job)

    @app.delete("/v1/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        authorize(request)
        job = await load(job_id)
        key = store.job_key(job_id)
        if job.get("status") == "running":
            await r.hset(key, "cancel", "1")
            return JSONResponse({"id": job_id, "status": "cancelling"}, status_code=202)
        if job.get("status") == "queued":
            await r.lrem(store.QUEUE, 0, job_id)
        if s3:
            keys = [k for k in (job.get("output_key"), f"jobs/{job_id}/input") if k]
            await run_in_threadpool(s3.delete_objects, Bucket=settings.s3_bucket,
                                    Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
        await r.delete(key)
        return {"id": job_id, "status": "deleted"}

    async def check_dependencies():
        if not settings.api_key_valid:
            log.warning("API_KEY is unusable: every /v1 request is refused until it is fixed")
        if not s3:
            log.warning("S3_* variables are empty: reference the Railway Bucket so uploads and outputs work")
            return
        try:
            await run_in_threadpool(s3.head_bucket, Bucket=settings.s3_bucket)
            log.info("Bucket %s reachable", settings.s3_bucket)
        except Exception as err:  # noqa: BLE001
            log.warning("Bucket check failed (%s); uploads and outputs will fail until it works",
                        type(err).__name__)

    return app
