from __future__ import annotations

import os
from dataclasses import asdict
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .adapters import Bitrix24Http, DemoBitrix, DemoSheets, GoogleSheetsHttp
from .domain import InMemoryQueue, PostgresQueue
from .service import SyncService


class SyncRequest(BaseModel):
    spreadsheet_id: str = Field(min_length=1, max_length=128)
    row: int = Field(ge=1, le=1_000_000)
    event_id: str = Field(min_length=1, max_length=256)


def build_service() -> SyncService:
    mode = os.getenv("ADAPTER_MODE", "demo")
    backend = os.getenv("QUEUE_BACKEND", "memory")
    queue = (PostgresQueue(os.environ["DATABASE_URL"], int(os.getenv("WORKER_LEASE_SECONDS", "60")))
             if backend == "postgres" else InMemoryQueue())
    if mode == "live":
        return SyncService(queue, GoogleSheetsHttp(os.environ["GOOGLE_API_KEY"]),
                            Bitrix24Http(os.environ["BITRIX_WEBHOOK_URL"], os.environ["BITRIX_ALLOWED_HOST"]))
    return SyncService(queue, DemoSheets({}), DemoBitrix())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.service = build_service()
    yield


app = FastAPI(title="SheetBridge Sync", version="0.1.0", lifespan=lifespan)


def service(request: Request) -> SyncService:
    return request.app.state.service


def admin_auth(x_admin_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("SYNC_ADMIN_TOKEN")
    required = os.getenv("REQUIRE_ADMIN_AUTH", "false").lower() == "true"
    if required and not expected:
        raise HTTPException(503, "admin auth is not configured")
    if expected and x_admin_token != expected:
        raise HTTPException(401, "invalid admin token")


def public_job(job) -> dict:
    data = asdict(job)
    data.pop("payload", None)  # rows may contain PII; inspect through audit storage in production
    return data


@app.get("/healthz")
def health(request: Request) -> dict[str, str]:
    service(request).queue.counts()  # verifies state repository is reachable
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    s = service(request)
    counts = s.queue.counts()
    lines = ["# TYPE sheetbridge_jobs gauge"]
    lines += [f'sheetbridge_jobs{{status="{key}"}} {value}' for key, value in counts.items()]
    lines += [f"sheetbridge_processed_total {s.processed}", f"sheetbridge_failed_total {s.failed}"]
    return "\n".join(lines) + "\n"


@app.post("/sync", status_code=202, dependencies=[Depends(admin_auth)])
def sync(body: SyncRequest, request: Request) -> dict:
    job, created = service(request).request_sync(body.spreadsheet_id, body.row, body.event_id)
    return {"job_id": job.id, "status": job.status, "created": created}


@app.post("/worker/tick", dependencies=[Depends(admin_auth)])
def tick(request: Request) -> dict:
    job = service(request).process_one()
    return {"job": public_job(job) if job else None}


@app.get("/jobs/{job_id}", dependencies=[Depends(admin_auth)])
def job(job_id: str, request: Request) -> dict:
    found = service(request).queue.get(job_id)
    if not found:
        raise HTTPException(404, "job not found")
    return public_job(found)


@app.get("/dlq", dependencies=[Depends(admin_auth)])
def dlq(request: Request) -> list[dict]:
    return [public_job(job) for job in service(request).queue.dlq()]


@app.post("/dlq/{job_id}/replay", dependencies=[Depends(admin_auth)])
def replay(job_id: str, request: Request) -> dict:
    try:
        job = service(request).queue.replay(job_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return public_job(job)
