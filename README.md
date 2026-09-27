# SheetBridge Sync

Сервис синхронизации строк Google Sheets с контактами Bitrix24 CRM.
Проект подходит для демонстрации интеграций, очередей, retry-политики и обработки ошибок.

## Возможности

- постановка синхронизации в очередь по `event_id`;
- защита от повторной обработки одного события;
- Google Sheets и Bitrix24 HTTP-адаптеры;
- повторные попытки для timeout, 408, 429 и 5xx;
- exponential backoff, jitter и `Retry-After`;
- DLQ для неисправимых задач и ручной replay;
- восстановление задач после истечения worker lease;
- healthcheck и Prometheus-метрики;
- скрытие payload строк из ответов API, чтобы не отдавать PII.

## Быстрый старт без внешних сервисов

Требуется Python 3.11+.

```bash
cd sheetbridge-sync
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
uvicorn app.main:app --reload
```

Сервис использует demo-адаптеры по умолчанию. В другом терминале создайте задачу:

```bash
curl -X POST http://localhost:8000/sync \
  -H 'Content-Type: application/json' \
  -d '{"spreadsheet_id":"demo","row":1,"event_id":"change-001"}'
```

Обработайте одну задачу:

```bash
curl -X POST http://localhost:8000/worker/tick
```

Проверьте состояние:

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/metrics
curl http://localhost:8000/jobs/<JOB_ID>
```

Swagger UI доступен по адресу <http://localhost:8000/docs>.

## Запуск через Docker

```bash
cp .env.example .env
docker compose up --build
```

В текущем MVP очередь хранится в памяти процесса, поэтому worker вызывается через
`POST /worker/tick`. Отдельный контейнер worker не запускается намеренно: он не сможет
разделить in-memory очередь с API. Для production следует подключить PostgreSQL-репозиторий
по схеме `migrations/001_initial.sql` и использовать `FOR UPDATE SKIP LOCKED`.

## Live-интеграции

В `.env` задайте:

```dotenv
ADAPTER_MODE=live
GOOGLE_API_KEY=your-google-sheets-api-key
BITRIX_WEBHOOK_URL=https://company.bitrix24.ru/rest/1/secret/
BITRIX_ALLOWED_HOST=company.bitrix24.ru
SYNC_ADMIN_TOKEN=long-random-secret
```

Для Google API включите Google Sheets API и выдайте ключу доступ к таблице. Строка должна
содержать:

| Колонка | Значение |
|---|---|
| A | имя контакта |
| B | email |

Запрос с авторизацией:

```bash
curl -X POST http://localhost:8000/sync \
  -H 'Content-Type: application/json' \
  -H 'X-Admin-Token: long-random-secret' \
  -d '{"spreadsheet_id":"1abc...","row":2,"event_id":"sheet-change-42"}'
```

`SYNC_ADMIN_TOKEN` защищает `/sync`, `/worker/tick`, `/jobs/*`, `/dlq` и replay.
Без этой переменной авторизация отключена только для локального demo-режима.
`/healthz` и `/metrics` остаются доступными для мониторинга.

Bitrix upsert сначала ищет контакт по стабильному `ORIGIN_ID`, затем обновляет или создаёт
его. Поэтому повтор после сетевой ошибки не должен создавать дубликат.

## Работа с ошибками и DLQ

Временные ошибки повторяются максимум пять раз. Постоянные ошибки валидации и ответы 4xx
сразу попадают в DLQ.

```bash
curl -H 'X-Admin-Token: long-random-secret' http://localhost:8000/dlq
curl -X POST -H 'X-Admin-Token: long-random-secret' \
  http://localhost:8000/dlq/<JOB_ID>/replay
```

Перед replay исправьте причину ошибки: credentials, доступ к таблице или данные строки.
Подробный runbook находится в [docs/runbook.md](docs/runbook.md).

## Тесты и структура

```bash
pytest -q
```

- `app/main.py` — FastAPI endpoints;
- `app/domain.py` — очередь, состояния и lease;
- `app/service.py` — orchestration и retry policy;
- `app/adapters.py` — demo и HTTP-интеграции;
- `migrations/001_initial.sql` — целевая PostgreSQL-схема;
- `docs/ADR-001-queue-and-delivery.md` — решение по доставке задач.

## Ограничения MVP

- Google Sheets опрашивается по запросу, полноценный scheduler не включён;
- in-memory очередь теряется при перезапуске процесса;
- OAuth Google/Bitrix и multi-tenant persistence требуют production-расширения;
- перед использованием с реальными данными настройте TLS, секрет-хранилище и внешний auth gateway.

## Лицензия

Проект распространяется по лицензии [MIT](LICENSE).
