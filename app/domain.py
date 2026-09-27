"""Small transactional-like in-memory domain; replace QueueRepository with Postgres in production."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Any
from uuid import uuid4


class Status(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    DLQ = "dlq"


@dataclass
class Job:
    id: str
    idempotency_key: str
    payload: dict[str, Any]
    status: Status = Status.PENDING
    attempts: int = 0
    run_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    lease_until: datetime | None = None


class InMemoryQueue:
    """All state transitions occur under one lock, mirroring an atomic repository transaction."""
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._keys: dict[str, str] = {}
        self._lock = RLock()

    def enqueue(self, key: str, payload: dict[str, Any]) -> tuple[Job, bool]:
        with self._lock:
            if key in self._keys:
                return self._jobs[self._keys[key]], False
            job = Job(id=str(uuid4()), idempotency_key=key, payload=payload)
            self._jobs[job.id], self._keys[key] = job, job.id
            return job, True

    def claim(self, now: datetime | None = None, lease_seconds: int = 60) -> Job | None:
        now = now or datetime.now(UTC)
        with self._lock:
            # Recover work abandoned by a crashed worker (at-least-once delivery).
            for abandoned in self._jobs.values():
                if abandoned.status == Status.PROCESSING and abandoned.lease_until and abandoned.lease_until <= now:
                    abandoned.status, abandoned.lease_until = Status.PENDING, None
            available = [j for j in self._jobs.values() if j.status == Status.PENDING and j.run_at <= now]
            if not available:
                return None
            job = min(available, key=lambda j: (j.run_at, j.created_at))
            job.status = Status.PROCESSING
            job.attempts += 1
            job.lease_until = now + timedelta(seconds=lease_seconds)
            return job

    def done(self, job_id: str) -> None:
        with self._lock:
            self._jobs[job_id].status = Status.DONE
            self._jobs[job_id].lease_until = None

    def fail(self, job_id: str, error: str, retry_at: datetime | None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.last_error = error[:1000]
            job.status = Status.PENDING if retry_at else Status.DLQ
            job.lease_until = None
            if retry_at:
                job.run_at = retry_at

    def replay(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs[job_id]
            if job.status != Status.DLQ:
                raise ValueError("only DLQ jobs can be replayed")
            job.status, job.run_at, job.last_error = Status.PENDING, datetime.now(UTC), None
            job.attempts = 0
            job.lease_until = None
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {s.value: sum(j.status == s for j in self._jobs.values()) for s in Status}

    def dlq(self) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status == Status.DLQ]
