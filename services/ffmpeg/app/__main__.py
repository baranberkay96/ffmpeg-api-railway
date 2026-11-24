"""Entry point: `python -m app api` (HTTP API) or `python -m app worker` (job worker).

The role can also come from ROLE; anything else falls back to the API with a warning.
"""
from __future__ import annotations

import logging
import os
import sys

from . import config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    log = logging.getLogger("ffmpeg")
    role = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ROLE", "api")).strip().lower()
    if role not in ("api", "worker"):
        log.warning("unknown role %r, starting the API", role)
        role = "api"
    settings = config.load(role)
    if role == "worker":
        from . import worker
        worker.main(settings)
        return
    import uvicorn

    from .api import create_app
    log.info("API on port %d, max input %d MB, sync wait %d s", settings.port,
             settings.max_input_bytes // 1048576, settings.sync_wait)
    # Single uvicorn worker on 0.0.0.0: Railway's healthcheck is IPv4 (STANDARD 8.2).
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, proxy_headers=True,
                forwarded_allow_ips="*", log_level="info", timeout_keep_alive=75)


if __name__ == "__main__":
    main()
