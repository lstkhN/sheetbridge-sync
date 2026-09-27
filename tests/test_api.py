from fastapi.testclient import TestClient
from app.main import app


def test_demo_api_end_to_end():
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        created = client.post("/sync", json={"spreadsheet_id": "demo", "row": 1, "event_id": "evt"})
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert client.post("/worker/tick").json()["job"]["status"] == "done"
        assert client.get(f"/jobs/{job_id}").json()["status"] == "done"
        assert "sheetbridge_jobs" in client.get("/metrics").text
