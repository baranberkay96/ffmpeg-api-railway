"""Worker: takes jobs from the Redis queue, runs FFmpeg with limits, stores the output.

FFmpeg runs as an unprivileged user with an empty environment and only the file/pipe
protocols, in a per-job temp directory, with a wall-clock timeout (JOB_TIMEOUT_SECONDS)
and an output size cap (MAX_OUTPUT_MB). A private /healthz on PORT serves Railway's
healthcheck (STANDARD 8.34).
"""
from __future__ import annotations

import collections
import http.server
import json
import logging
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import zipfile

import redis

from . import fetch, ops, store
from .config import Settings

log = logging.getLogger("ffmpeg.worker")
JOB_USER = "ffmpegjob"
PLAYLIST_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "dash", "imf", "smil"}
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
STOP = threading.Event()


class JobError(Exception):
    """A job failed for a reason the client should see."""


def _job_ids() -> tuple[int, int] | None:
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(JOB_USER)
        return entry.pw_uid, entry.pw_gid
    except KeyError:
        return None


class Worker:
    def __init__(self, settings: Settings):
        self.s = settings
        self.r = redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=30,
                                      socket_connect_timeout=10, health_check_interval=30)
        self.s3 = store.s3_client(settings) if settings.s3_configured else None
        self.ids = _job_ids()
        os.makedirs(settings.work_dir, exist_ok=True)

    # ---------- process helpers ----------
    def _popen(self, argv: list[str], cwd: str, **kwargs) -> subprocess.Popen:
        extra = {}
        if self.ids:
            extra = {"user": self.ids[0], "group": self.ids[1], "extra_groups": []}
        return subprocess.Popen(argv, cwd=cwd, env={"PATH": SAFE_PATH, "HOME": cwd}, stdin=subprocess.DEVNULL,
                                start_new_session=True, **extra, **kwargs)

    def _probe(self, path: str, cwd: str) -> dict:
        argv = ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-print_format", "json",
                "-show_format", "-show_streams", path]
        proc = self._popen(argv, cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise JobError("ffprobe timed out reading the input") from None
        if proc.returncode != 0:
            raise JobError("input is not a media file FFmpeg can read: " + _tail(err.decode(errors="replace")))
        try:
            return json.loads(out)
        except ValueError:
            raise JobError("ffprobe returned no metadata") from None

    def _run_ffmpeg(self, job_id: str, argv: list[str], cwd: str, total_seconds: float | None) -> None:
        proc = self._popen(argv, cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        errors: collections.deque[str] = collections.deque(maxlen=20)
        reader = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True)
        reader.start()
        state = {"progress": 0.0, "reason": None}
        done = threading.Event()

        def watchdog() -> None:
            # Heartbeat, progress, cancel and deadline, even while FFmpeg prints nothing.
            deadline = time.monotonic() + self.s.job_timeout
            while not done.wait(2):
                try:
                    self.r.hset(store.job_key(job_id), "progress", round(state["progress"], 1))
                    self.r.set(store.heartbeat_key(job_id), "1", ex=60)
                    if self.r.hget(store.job_key(job_id), "cancel") == "1":
                        state["reason"] = "cancelled"
                except redis.RedisError:
                    pass
                if time.monotonic() > deadline:
                    state["reason"] = "timeout"
                if state["reason"]:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

        guard = threading.Thread(target=watchdog, daemon=True)
        guard.start()
        for line in proc.stdout:  # -progress pipe:1 writes key=value lines
            if line.startswith("out_time_us=") and total_seconds:
                try:
                    state["progress"] = max(0.0, min(99.0, int(line.split("=", 1)[1]) / 1e6 / total_seconds * 100))
                except ValueError:
                    pass
        proc.wait()
        done.set()
        guard.join(timeout=5)
        reader.join(timeout=5)
        if state["reason"] == "timeout":
            raise JobError(f"job exceeded JOB_TIMEOUT_SECONDS ({self.s.job_timeout} s) and was stopped")
        if state["reason"] == "cancelled":
            raise JobError("cancelled")
        if proc.returncode != 0:
            text = "".join(errors)
            if "received no packets" in text or "Nothing was written" in text:
                raise JobError("no frames or samples in the requested time range (check time/start/duration)")
            raise JobError("ffmpeg failed: " + _tail(text))

    # ---------- job processing ----------
    def process(self, job_id: str) -> None:
        key = store.job_key(job_id)
        job = store.decode(self.r.hgetall(key))
        if not job or job.get("status") != "queued":
            return
        self.r.hset(key, mapping={"status": "running", "started_at": store.now(), "progress": 0})
        self.r.sadd(store.RUNNING, job_id)
        self.r.set(store.heartbeat_key(job_id), "1", ex=60)
        workdir = tempfile.mkdtemp(prefix=f"{job_id}-", dir=self.s.work_dir)
        status, fields = "failed", {}
        try:
            fields = self._execute(job, workdir)
            status = "done"
        except JobError as err:
            status = "cancelled" if str(err) == "cancelled" else "failed"
            fields = {"error": str(err)[:2000]}
        except fetch.FetchError as err:
            fields = {"error": str(err)[:2000]}
        except Exception:  # noqa: BLE001 - keep the worker alive, report a generic error
            log.exception("job %s crashed", job_id)
            fields = {"error": "internal error while processing the job (see worker logs)"}
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if job.get("source") == "upload" and self.s3:
                try:
                    self.s3.delete_object(Bucket=self.s.s3_bucket, Key=f"jobs/{job_id}/input")
                except Exception:  # noqa: BLE001
                    log.warning("could not delete the input of job %s", job_id)
        fields.update(status=status, finished_at=store.now())
        if status == "done":
            fields["progress"] = 100
        if self.r.exists(key):
            self.r.hset(key, mapping=fields)
        self.r.srem(store.RUNNING, job_id)
        self.r.delete(store.heartbeat_key(job_id))
        log.info("job %s %s (%s)", job_id, status, job.get("operation"))
        if job.get("callback_url"):
            final = store.public_view(store.decode(self.r.hgetall(key)) or {**job, **fields})
            code = fetch.post_json(job["callback_url"], final, self.s.allow_private_urls)
            self.r.hset(key, "callback_status", code)

    def _execute(self, job: dict, workdir: str) -> dict:
        if self.ids:
            os.chown(workdir, *self.ids)
        options = job.get("options") or {}
        suffix = _safe_suffix(job.get("input_name") or "")
        input_path = os.path.join(workdir, "input" + suffix)
        if job.get("source") == "upload":
            if not self.s3:
                raise JobError("the Bucket is not configured (S3_* variables)")
            self.s3.download_file(self.s.s3_bucket, f"jobs/{job['id']}/input", input_path)
        else:
            name = fetch.download(job["input_url"], input_path, self.s.max_input_bytes, self.s.allow_private_urls)
            if not job.get("input_name") and name:
                self.r.hset(store.job_key(job["id"]), "input_name", name[:200])
        if self.ids:
            os.chown(input_path, *self.ids)
        info = self._probe(input_path, workdir)
        formats = set((info.get("format") or {}).get("format_name", "").split(","))
        if formats & PLAYLIST_FORMATS:
            raise JobError("playlists (HLS, DASH, concat lists) are not accepted; send the media file itself")
        if options.get("operation") == "probe":
            return {"result": json.dumps(info)[:512_000]}
        plan = ops.plan(options, self.s.threads)
        out_dir = os.path.join(workdir, "out")
        os.mkdir(out_dir)
        if self.ids:
            os.chown(out_dir, *self.ids)
        target = os.path.join(out_dir, f"frame_%05d.{plan.extension}" if plan.multi_file else f"output.{plan.extension}")
        argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
                "-protocol_whitelist", "file,pipe", *plan.input_args, "-i", input_path, *plan.output_args,
                "-fs", str(self.s.max_output_bytes), "-y", target]
        total = _duration(info, options)
        self._run_ffmpeg(job["id"], argv, workdir, total)
        output, ext = self._collect(out_dir, plan)
        size = os.path.getsize(output)
        if size >= self.s.max_output_bytes:
            raise JobError(f"output reached MAX_OUTPUT_MB ({self.s.max_output_bytes // 1048576} MB) and was cut off")
        name = _output_name(options.get("filename") or job.get("filename"), job.get("input_name"), ext,
                            options.get("operation"))
        content_type = ops.CONTENT_TYPES.get(ext, "application/octet-stream")
        key = f"jobs/{job['id']}/{name}"
        self.s3.upload_file(output, self.s.s3_bucket, key, ExtraArgs={"ContentType": content_type})
        return {"output_key": key, "output_name": name, "output_size": size, "content_type": content_type}

    @staticmethod
    def _collect(out_dir: str, plan: ops.Plan) -> tuple[str, str]:
        files = sorted(f for f in os.listdir(out_dir) if not f.startswith("."))
        if not files:
            raise JobError("ffmpeg produced no output (is the requested time inside the media?)")
        if not plan.multi_file:
            return os.path.join(out_dir, files[0]), plan.extension
        archive = os.path.join(os.path.dirname(out_dir), "frames.zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            for name in files:
                zf.write(os.path.join(out_dir, name), name)
        return archive, "zip"

    # ---------- maintenance ----------
    def maintenance(self) -> None:
        """Every 10 min (one worker at a time): fail jobs whose worker vanished, delete old outputs."""
        if not self.r.set(store.PREFIX + "maintenance-lock", "1", nx=True, ex=600):
            return
        for job_id in self.r.smembers(store.RUNNING):
            if not self.r.exists(store.heartbeat_key(job_id)):
                key = store.job_key(job_id)
                if self.r.hget(key, "status") == "running":
                    self.r.hset(key, mapping={"status": "failed", "finished_at": store.now(),
                                              "error": "the worker stopped while processing (redeploy or out of memory)"})
                self.r.srem(store.RUNNING, job_id)
        if not self.s3:
            return
        cutoff = time.time() - self.s.retention_hours * 3600
        deleted = 0
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.s.s3_bucket, Prefix="jobs/"):
            old = [{"Key": o["Key"]} for o in page.get("Contents", []) if o["LastModified"].timestamp() < cutoff]
            for i in range(0, len(old), 1000):
                self.s3.delete_objects(Bucket=self.s.s3_bucket, Delete={"Objects": old[i:i + 1000], "Quiet": True})
                deleted += len(old[i:i + 1000])
        if deleted:
            log.info("maintenance: deleted %d bucket objects older than %d h", deleted, self.s.retention_hours)

    def loop(self, slot: int) -> None:
        while not STOP.is_set():
            try:
                item = self.r.blpop([store.QUEUE], timeout=5)
                if item:
                    self.process(item[1])
            except redis.RedisError as err:
                log.warning("worker %d: Redis unavailable (%s), retrying in 5 s", slot, err)
                STOP.wait(5)

    def maintenance_loop(self) -> None:
        while not STOP.wait(60):
            try:
                self.maintenance()
            except Exception as err:  # noqa: BLE001
                log.warning("maintenance failed: %s", err)


def _tail(text: str, limit: int = 600) -> str:
    text = re.sub(r"\S*/((input|output|frame_%05d)(\.[A-Za-z0-9]+)?)\b", r"\1", text.strip())
    text = re.sub(r"\s*\[[^\]]*@ 0x[0-9a-f]+\]\s*", " ", text)
    return text[-limit:] or "no error output"


def _safe_suffix(name: str) -> str:
    match = re.search(r"\.([A-Za-z0-9]{1,8})$", name)
    return "." + match.group(1).lower() if match else ""


def _duration(info: dict, options: dict) -> float | None:
    if options.get("duration"):
        return ops._seconds(options["duration"])
    try:
        total = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    if options.get("start"):
        total -= ops._seconds(options["start"])
    return total if total > 0 else None


def _output_name(requested: str | None, input_name: str | None, ext: str, operation: str | None) -> str:
    base = requested or ""
    if not base:
        stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", input_name or "") or "output"
        base = f"{stem}-{operation}" if operation in ("thumbnail", "frames", "gif") else stem
    base = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", base)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "output"
    return f"{base[:100]}.{ext}"


class _DualStackServer(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


class _Health(http.server.BaseHTTPRequestHandler):
    redis_client: redis.Redis | None = None

    def do_GET(self) -> None:  # noqa: N802
        ok = False
        try:
            ok = bool(self.redis_client and self.redis_client.ping())
        except redis.RedisError:
            ok = False
        body = json.dumps({"status": "ok" if ok else "redis unavailable"}).encode()
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def main(settings: Settings) -> None:
    worker = Worker(settings)
    if not settings.s3_configured:
        log.warning("S3_* variables are empty: upload jobs and outputs need the Railway Bucket. "
                    "Reference the Bucket variables in this service.")
    if not worker.ids:
        log.warning("not running as root or user %s missing: FFmpeg runs as the worker's own user", JOB_USER)
    _Health.redis_client = worker.r
    server = _DualStackServer(("::", settings.port), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("worker ready: %d job slot(s), %d FFmpeg thread(s), timeout %d s, max input %d MB, max output %d MB",
             settings.concurrency, settings.threads, settings.job_timeout,
             settings.max_input_bytes // 1048576, settings.max_output_bytes // 1048576)
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    threads = [threading.Thread(target=worker.loop, args=(i,), daemon=True) for i in range(settings.concurrency)]
    threads.append(threading.Thread(target=worker.maintenance_loop, daemon=True))
    for t in threads:
        t.start()
    while not STOP.wait(1):
        pass
    log.info("worker stopping")
