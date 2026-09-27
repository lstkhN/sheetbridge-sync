from app.adapters import DemoBitrix, DemoSheets, TransientIntegrationError
from app.domain import InMemoryQueue, Status
from app.service import RetryPolicy, SyncService


def test_idempotency_and_success():
    crm = DemoBitrix()
    svc = SyncService(InMemoryQueue(), DemoSheets({}), crm)
    first, created = svc.request_sync("sheet", 2, "event-1")
    same, duplicate = svc.request_sync("sheet", 2, "event-1")
    assert created and not duplicate and first.id == same.id
    assert svc.process_one().status == Status.DONE
    assert len(crm.writes) == 1


def test_transient_failure_retries_then_dlq():
    class BrokenSheets:
        def get_row(self, spreadsheet_id, row):
            raise TransientIntegrationError("rate limited", retry_after=0)
    svc = SyncService(InMemoryQueue(), BrokenSheets(), DemoBitrix(), RetryPolicy(max_attempts=1))
    job, _ = svc.request_sync("s", 1, "e")
    assert svc.process_one().status == Status.DLQ
    assert svc.queue.dlq()[0].id == job.id
    assert svc.queue.replay(job.id).status == Status.PENDING


def test_retry_policy_honors_retry_after():
    policy = RetryPolicy(max_attempts=3)
    assert policy.next_run(3) is None
    assert policy.next_run(1, retry_after=0) is not None
