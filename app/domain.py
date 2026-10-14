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


class PostgresQueue:
    """Durable queue for production deployments.

    Each claim is performed in a short transaction with SKIP LOCKED. Delivery is
    at-least-once: an expired lease is returned to pending and may be processed again.
    """

    def __init__(self, dsn: str, lease_seconds: int = 60) -> None:
        import psycopg

        self._psycopg = psycopg
        self.dsn = dsn
        self.lease_seconds = lease_seconds

    def _job(self, row) -> Job:
        return Job(
            id=str(row["id"]), idempotency_key=row["idempotency_key"], payload=row["payload"],
            status=Status(row["status"]), attempts=row["attempts"], run_at=row["run_at"],
            last_error=row["last_error"], created_at=row["created_at"], lease_until=row["lease_until"],
        )

    def enqueue(self, key: str, payload: dict[str, Any]) -> tuple[Job, bool]:
        with self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row) as conn:
            payload = self._psycopg.types.json.Jsonb(payload)
            row = conn.execute(
                """INSERT INTO sync_jobs (id, idempotency_key, payload)
                   VALUES (gen_random_uuid(), %s, %s)
                   ON CONFLICT (idempotency_key) DO NOTHING
                   RETURNING *""", (key, payload),
            ).fetchone()
            if row:
                return self._job(row), True
            row = conn.execute("SELECT * FROM sync_jobs WHERE idempotency_key = %s", (key,)).fetchone()
            return self._job(row), False

    def claim(self, now: datetime | None = None, lease_seconds: int | None = None) -> Job | None:
        now = now or datetime.now(UTC)
        lease = lease_seconds or self.lease_seconds
        with self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row) as conn:
            conn.execute(
                "UPDATE sync_jobs SET status = 'pending', lease_until = NULL "
                "WHERE status = 'processing' AND lease_until <= %s", (now,),
            )
            row = conn.execute(
                """SELECT * FROM sync_jobs
                   WHERE status = 'pending' AND run_at <= %s
                   ORDER BY run_at, created_at
                   FOR UPDATE SKIP LOCKED LIMIT 1""", (now,),
            ).fetchone()
            if not row:
                return None
            row = conn.execute(
                """UPDATE sync_jobs
                   SET status = 'processing', attempts = attempts + 1,
                       lease_until = %s
                   WHERE id = %s RETURNING *""",
                (now + timedelta(seconds=lease), row["id"]),
            ).fetchone()
            return self._job(row)

    def done(self, job_id: str) -> None:
        with self._psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE sync_jobs SET status = 'done', lease_until = NULL WHERE id = %s", (job_id,))

    def fail(self, job_id: str, error: str, retry_at: datetime | None) -> None:
        with self._psycopg.connect(self.dsn) as conn:
            conn.execute(
                """UPDATE sync_jobs SET last_error = %s, status = %s,
                   lease_until = NULL, run_at = COALESCE(%s, run_at) WHERE id = %s""",
                (error[:1000], "pending" if retry_at else "dlq", retry_at, job_id),
            )

    def replay(self, job_id: str) -> Job:
        with self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row) as conn:
            row = conn.execute(
                """UPDATE sync_jobs SET status = 'pending', run_at = now(),
                   last_error = NULL, attempts = 0, lease_until = NULL
                   WHERE id = %s AND status = 'dlq' RETURNING *""", (job_id,),
            ).fetchone()
            if not row:
                existing = conn.execute("SELECT status FROM sync_jobs WHERE id = %s", (job_id,)).fetchone()
                if not existing:
                    raise KeyError(job_id)
                raise ValueError("only DLQ jobs can be replayed")
            return self._job(row)

    def get(self, job_id: str) -> Job | None:
        with self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row) as conn:
            row = conn.execute("SELECT * FROM sync_jobs WHERE id = %s", (job_id,)).fetchone()
            return self._job(row) if row else None

    def counts(self) -> dict[str, int]:
        with self._psycopg.connect(self.dsn) as conn:
            rows = conn.execute("SELECT status, count(*) FROM sync_jobs GROUP BY status").fetchall()
            values = {status.value: 0 for status in Status}
            values.update({str(status): count for status, count in rows})
            return values

    def dlq(self) -> list[Job]:
        with self._psycopg.connect(self.dsn, row_factory=self._psycopg.rows.dict_row) as conn:
            return [self._job(row) for row in conn.execute(
                "SELECT * FROM sync_jobs WHERE status = 'dlq' ORDER BY created_at"
            ).fetchall()]
