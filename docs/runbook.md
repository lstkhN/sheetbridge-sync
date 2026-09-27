# Runbook

## Проверка
`GET /healthz` должен вернуть `{"status":"ok"}`. Метрики: `GET /metrics`; контролируйте `sheetbridge_jobs{status="dlq"}` и `sheetbridge_failed_total`.

## DLQ
1. Посмотрите `GET /dlq` и устраните причину (данные/доступ/лимит).
2. После исправления: `POST /dlq/{job_id}/replay`.
3. В MVP выполните `POST /worker/tick`; production worker забирает готовые задания постоянно.

## Инцидент
При 429/5xx/timeout задача повторяется с exponential backoff, jitter и `Retry-After`. Ошибки 4xx (кроме 408/429) направляются в DLQ. Не вставляйте webhook URL, API keys или персональные данные в логи/тикеты.
