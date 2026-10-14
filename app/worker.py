"""Long-running production worker.

The API and worker use the same PostgreSQL queue. Multiple worker replicas are safe:
PostgresQueue.claim uses FOR UPDATE SKIP LOCKED.
"""
from __future__ import annotations

import logging
import os
import time

from .main import build_service

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("sheetbridge.worker")


def main() -> None:
    if os.getenv("QUEUE_BACKEND", "memory") != "postgres":
        raise RuntimeError("production worker requires QUEUE_BACKEND=postgres")
    service = build_service()
    poll = float(os.getenv("WORKER_POLL_SECONDS", "1"))
    log.info("worker started")
    while True:
        job = service.process_one()
        if job is None:
            time.sleep(poll)
        else:
            log.info("processed job=%s status=%s attempts=%s", job.id, job.status, job.attempts)


if __name__ == "__main__":
    main()
