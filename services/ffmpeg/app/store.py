"""Job records in Redis and files in the Railway Bucket (S3 API).

Redis keys (prefix ffmpeg:):
  job:<id>      hash with the job record, expires after RETENTION_HOURS
  queue         list of queued job ids (RPUSH by the API, BLPOP by workers)
  running       set of job ids a worker is processing
  hb:<id>       heartbeat of a running job (expires 60 s after the worker stops)
Bucket keys: jobs/<id>/input (uploads, deleted when the job ends) and jobs/<id>/<file>.
"""
from __future__ import annotations

import json
import time

import boto3
from botocore.config import Config

from .config import Settings

PREFIX = "ffmpeg:"
QUEUE = PREFIX + "queue"
RUNNING = PREFIX + "running"
FINAL = ("done", "failed", "cancelled")


def job_key(job_id: str) -> str:
    return f"{PREFIX}job:{job_id}"


def heartbeat_key(job_id: str) -> str:
    return f"{PREFIX}hb:{job_id}"


def now() -> float:
    return round(time.time(), 3)


def s3_client(settings: Settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_key_id,
        aws_secret_access_key=settings.s3_secret,
        # Railway Buckets use virtual-hosted-style URLs.
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"},
                      retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=10, read_timeout=120),
    )


def decode(record: dict) -> dict:
    """Redis hash (strings) -> job dict with parsed JSON fields."""
    job = dict(record)
    for field in ("options", "result"):
        if job.get(field):
            try:
                job[field] = json.loads(job[field])
            except ValueError:
                pass
    for field in ("created_at", "started_at", "finished_at", "progress", "output_size"):
        if job.get(field) not in (None, ""):
            try:
                job[field] = float(job[field])
            except ValueError:
                pass
    return job


def public_view(job: dict, presigned: str | None = None, expires_in: int | None = None) -> dict:
    """What clients see: no bucket keys, no internal fields."""
    view = {k: job.get(k) for k in ("id", "status", "operation", "options", "source", "input_name", "error",
                                    "created_at", "started_at", "finished_at", "progress")}
    view["options"] = {k: v for k, v in (job.get("options") or {}).items() if k != "operation"}
    if job.get("status") == "done" and job.get("output_key"):
        view["output"] = {
            "filename": job.get("output_name"),
            "size": int(job.get("output_size") or 0),
            "content_type": job.get("content_type"),
            "download_path": f"/v1/jobs/{job['id']}/output",
        }
        if presigned:
            view["output"]["url"] = presigned
            view["output"]["url_expires_in"] = expires_in
    if job.get("result") is not None:
        view["result"] = job["result"]
    return view
