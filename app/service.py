from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from .adapters import CrmWriter, SheetReader, TransientIntegrationError
from .domain import InMemoryQueue, Job


class RetryPolicy:
    def __init__(self, max_attempts: int = 5, base_seconds: float = 2, cap_seconds: float = 300) -> None:
        self.max_attempts, self.base_seconds, self.cap_seconds = max_attempts, base_seconds, cap_seconds

    def next_run(self, attempt: int, retry_after: float | None = None) -> datetime | None:
        if attempt >= self.max_attempts:
            return None
        delay = retry_after if retry_after is not None else min(self.cap_seconds, self.base_seconds * 2 ** (attempt - 1))
        delay += random.uniform(0, min(delay * 0.2, 10))  # full service avoids synchronized retries
        return datetime.now(UTC) + timedelta(seconds=delay)


class SyncService:
    def __init__(self, queue: InMemoryQueue, sheets: SheetReader, crm: CrmWriter, retry: RetryPolicy | None = None) -> None:
        self.queue, self.sheets, self.crm = queue, sheets, crm
        self.retry = retry or RetryPolicy()
        self.processed = self.failed = 0

    def request_sync(self, spreadsheet_id: str, row: int, event_id: str) -> tuple[Job, bool]:
        # Event ID from Sheets webhook/change feed is the business idempotency boundary.
        return self.queue.enqueue(f"sheet:{spreadsheet_id}:event:{event_id}", {"spreadsheet_id": spreadsheet_id, "row": row})

    def process_one(self) -> Job | None:
        job = self.queue.claim()
        if not job:
            return None
        try:
            record = self.sheets.get_row(job.payload["spreadsheet_id"], job.payload["row"])
            self.crm.upsert_portfolio(record, job.idempotency_key)
        except TransientIntegrationError as exc:
            self.queue.fail(job.id, str(exc), self.retry.next_run(job.attempts, exc.retry_after))
            self.failed += 1
        except Exception as exc:
            # Validation and 4xx adapters must not be retried indefinitely.
            self.queue.fail(job.id, str(exc), None)
            self.failed += 1
        else:
            self.queue.done(job.id)
            self.processed += 1
        return self.queue.get(job.id)
