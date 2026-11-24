"""Settings from environment variables.

Every value typed into the Railway deploy form is validated here. A bad value never
stops the service (STANDARD 8.29): it falls back to the default with a warning, and an
unusable API key locks the API with a random key nobody knows instead of opening it.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from dataclasses import dataclass

log = logging.getLogger("ffmpeg")

MB = 1024 * 1024


def _warn(name: str, value: str, fallback: object, why: str) -> None:
    log.warning("%s=%r %s, using %s", name, value, why, fallback)


def env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(float(raw))
    except ValueError:
        _warn(name, raw, default, "is not a number")
        return default
    if value < low or value > high:
        clamped = min(max(value, low), high)
        _warn(name, raw, clamped, f"is outside {low}..{high}")
        return clamped
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    _warn(name, raw, default, "is not true/false")
    return default


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _api_key() -> tuple[str, bool]:
    key = os.environ.get("API_KEY", "")
    if 16 <= len(key) <= 256 and re.fullmatch(r"[\x21-\x7e]+", key):
        return key, True
    log.warning("API_KEY is empty, shorter than 16 or longer than 256 characters, or contains spaces.")
    log.warning("The API is LOCKED with a random key nobody knows (only /healthz stays open).")
    log.warning("Fix: set API_KEY to 16-256 printable characters without spaces in the Variables tab and redeploy.")
    return secrets.token_urlsafe(48), False


def cgroup_cpus() -> float | None:
    """CPU quota of this container (cgroup v2 cpu.max), None when unlimited/unknown."""
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max":
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    return None


def default_threads() -> int:
    # Railway hosts expose ~96 cores; size the pool from the quota, capped (STANDARD 8.15).
    cpus = cgroup_cpus() or float(os.cpu_count() or 2)
    return max(1, min(8, int(round(cpus))))


@dataclass(frozen=True)
class Settings:
    role: str
    port: int
    api_key: str
    api_key_valid: bool
    redis_url: str
    s3_endpoint: str
    s3_region: str
    s3_bucket: str
    s3_key_id: str
    s3_secret: str
    max_input_bytes: int
    max_output_bytes: int
    job_timeout: int
    sync_wait: int
    max_queued: int
    retention_hours: int
    presign_seconds: int
    concurrency: int
    threads: int
    allow_private_urls: bool
    allow_custom_args: bool
    work_dir: str

    @property
    def s3_configured(self) -> bool:
        return all((self.s3_endpoint, self.s3_bucket, self.s3_key_id, self.s3_secret))


def load(role: str) -> Settings:
    port = env_int("PORT", 8080, 1, 65535)
    api_key, valid = _api_key() if role == "api" else ("", False)
    threads = env_int("FFMPEG_THREADS", 0, 0, 64) or default_threads()
    return Settings(
        role=role,
        port=port,
        api_key=api_key,
        api_key_valid=valid,
        redis_url=env_str("REDIS_URL", "redis://localhost:6379"),
        s3_endpoint=env_str("S3_ENDPOINT"),
        s3_region=env_str("S3_REGION", "auto"),
        s3_bucket=env_str("S3_BUCKET"),
        s3_key_id=env_str("S3_ACCESS_KEY_ID"),
        s3_secret=os.environ.get("S3_SECRET_ACCESS_KEY", "").strip(),
        max_input_bytes=env_int("MAX_INPUT_MB", 500, 1, 50_000) * MB,
        max_output_bytes=env_int("MAX_OUTPUT_MB", 2000, 1, 50_000) * MB,
        job_timeout=env_int("JOB_TIMEOUT_SECONDS", 1800, 10, 86_400),
        sync_wait=env_int("SYNC_WAIT_SECONDS", 300, 0, 840),
        max_queued=env_int("MAX_QUEUED_JOBS", 100, 1, 100_000),
        retention_hours=env_int("RETENTION_HOURS", 24, 1, 2160),
        presign_seconds=env_int("PRESIGNED_URL_SECONDS", 3600, 60, 604_800),
        concurrency=env_int("WORKER_CONCURRENCY", 1, 1, 16),
        threads=threads,
        allow_private_urls=env_bool("ALLOW_PRIVATE_URLS", False),
        allow_custom_args=env_bool("ALLOW_CUSTOM_ARGS", True),
        work_dir=env_str("WORK_DIR", "/tmp/ffmpeg-jobs"),
    )
