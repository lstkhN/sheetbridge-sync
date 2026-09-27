from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class TransientIntegrationError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentIntegrationError(Exception):
    pass


class SheetReader(Protocol):
    def get_row(self, spreadsheet_id: str, row: int) -> dict[str, str]: ...


class CrmWriter(Protocol):
    def upsert_portfolio(self, record: dict[str, str], idempotency_key: str) -> str: ...


@dataclass
class DemoSheets:
    rows: dict[tuple[str, int], dict[str, str]]

    def get_row(self, spreadsheet_id: str, row: int) -> dict[str, str]:
        return self.rows.get((spreadsheet_id, row), {"name": f"Demo portfolio #{row}", "email": f"demo{row}@example.test"})


class DemoBitrix:
    def __init__(self) -> None:
        self.writes: dict[str, dict[str, str]] = {}

    def upsert_portfolio(self, record: dict[str, str], idempotency_key: str) -> str:
        self.writes.setdefault(idempotency_key, record)
        return f"demo-{idempotency_key[:12]}"


class SafeHttpClient:
    """HTTP base with explicit host allowlist, bounded timeouts and retryable classification."""
    def __init__(self, base_url: str, allowed_hosts: set[str], token: str | None = None) -> None:
        host = urlparse(base_url).hostname
        if urlparse(base_url).scheme != "https" or host not in allowed_hosts:
            raise ValueError("integration URL must be HTTPS and match its configured allowlist")
        self.base_url, self.token = base_url.rstrip("/"), token
        self.client = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, f"{self.base_url}/{path.lstrip('/')}", **kwargs)
        except httpx.TimeoutException as exc:
            raise TransientIntegrationError("upstream timeout") from exc
        except httpx.TransportError as exc:
            raise TransientIntegrationError("upstream transport error") from exc
        if response.status_code in (408, 429) or response.status_code >= 500:
            retry = response.headers.get("Retry-After")
            delay = None
            if retry:
                try:
                    delay = float(retry)
                except ValueError:
                    try:
                        delay = max(0.0, (parsedate_to_datetime(retry) - datetime.now(UTC)).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        delay = None
            raise TransientIntegrationError(f"upstream HTTP {response.status_code}", min(delay, 300.0) if delay is not None else None)
        if response.status_code >= 400:
            raise PermanentIntegrationError(f"upstream HTTP {response.status_code}")
        return response


class GoogleSheetsHttp(SafeHttpClient):
    def __init__(self, api_key: str) -> None:
        super().__init__("https://sheets.googleapis.com/v4", {"sheets.googleapis.com"}, api_key)

    def get_row(self, spreadsheet_id: str, row: int) -> dict[str, str]:
        response = self.request("GET", f"spreadsheets/{spreadsheet_id}/values/A{row}:Z{row}", params={"key": self.token})
        try:
            body = response.json()
            values = body["values"][0]
            name, email = str(values[0]).strip(), str(values[1]).strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PermanentIntegrationError("Sheets response has invalid row schema") from exc
        if not name or "@" not in email:
            raise PermanentIntegrationError("row must contain non-empty name and valid email")
        return {"name": name, "email": email}


class Bitrix24Http(SafeHttpClient):
    def __init__(self, webhook_url: str, allowed_host: str) -> None:
        # Bitrix webhook URLs contain the secret path; never log this URL.
        super().__init__(webhook_url, {allowed_host})

    def upsert_portfolio(self, record: dict[str, str], idempotency_key: str) -> str:
        # CRM entity/schema is deployment-specific; this demonstrates a safe REST request shape.
        external_id = f"sheetbridge:{idempotency_key}"
        # Bitrix does not guarantee Idempotency-Key semantics: locate the stable
        # external key first, then update or create. A timeout is therefore safe to retry.
        lookup = self.request("POST", "crm.contact.list.json", json={"filter": {"ORIGIN_ID": external_id}, "select": ["ID"]})
        found = lookup.json().get("result", [])
        fields = {"NAME": record["name"], "ORIGIN_ID": external_id,
                  "EMAIL": [{"VALUE": record["email"], "VALUE_TYPE": "WORK"}]}
        if found:
            contact_id = str(found[0]["ID"])
            response = self.request("POST", f"crm.contact.update.json", json={"id": contact_id, "fields": fields})
            return contact_id
        response = self.request("POST", "crm.contact.add.json", json={"fields": fields, "params": {"REGISTER_SONET_EVENT": "N"}})
        try:
            return str(response.json()["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise PermanentIntegrationError("Bitrix response has invalid result") from exc
