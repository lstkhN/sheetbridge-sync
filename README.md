# SheetBridge Sync

Показательный production-ready сервис для синхронизации строки Google Sheets с контактом
Bitrix24 CRM. Сервис принимает события об изменениях, сохраняет их в надёжную очередь
PostgreSQL и обрабатывает отдельными worker-процессами. Повторная доставка одного события
идемпотентна и не создаёт дубликаты контактов.

## Архитектура

```text
Клиент → API → очередь PostgreSQL ← Worker(ы) → Google Sheets
                                      │
                                      └──────────→ Bitrix24
```

- **API** принимает запросы синхронизации и предоставляет healthcheck, метрики и операции DLQ.
- **PostgreSQL** хранит задачи, ключи идемпотентности, состояние повторов и lease.
- **Worker** забирает задачи через `FOR UPDATE SKIP LOCKED`; можно запускать несколько реплик.
- **Адаптеры** поддерживают `demo`-режим для разработки и HTTPS-интеграции в `live`-режиме.
- Доставка выполняется по принципу **at-least-once**. Upsert Bitrix24 использует стабильный
  `ORIGIN_ID`, поэтому повторная обработка безопасна.

## Быстрый запуск через Docker

Требуются Docker и Docker Compose.

```bash
git clone https://github.com/lstkhN/sheetbridge-sync.git
cd sheetbridge-sync
cp .env.example .env
# Измените POSTGRES_PASSWORD, DATABASE_URL и SYNC_ADMIN_TOKEN в .env.
docker compose up --build -d
```

Запускаются PostgreSQL, API и постоянно работающий worker. Миграция применяется автоматически
при первом создании PostgreSQL volume. API доступен по адресу <http://localhost:8000>.

Создайте и проверьте demo-задачу:

```bash
export TOKEN='replace-with-a-long-random-secret'
curl -X POST http://localhost:8000/sync \
  -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"spreadsheet_id":"demo","row":1,"event_id":"change-001"}'
curl -H "X-Admin-Token: $TOKEN" http://localhost:8000/healthz
curl -H "X-Admin-Token: $TOKEN" http://localhost:8000/metrics
```

`POST /sync` возвращает `job_id`, после чего worker автоматически обработает задачу.
Swagger UI доступен по адресу <http://localhost:8000/docs>.

> В настоящем deployment не открывайте API напрямую в Интернет. Используйте TLS,
> внешний authentication gateway и управляемый PostgreSQL или зашифрованный volume.

## Live-интеграции

Задайте в `.env`:

```dotenv
ADAPTER_MODE=live
QUEUE_BACKEND=postgres
DATABASE_URL=postgresql://sheetbridge:<password>@db:5432/sheetbridge
REQUIRE_ADMIN_AUTH=true
SYNC_ADMIN_TOKEN=<long-random-secret>
GOOGLE_API_KEY=<google-sheets-api-key>
BITRIX_WEBHOOK_URL=https://company.bitrix24.ru/rest/1/secret/
BITRIX_ALLOWED_HOST=company.bitrix24.ru
```

### Настройка Google Sheets

1. Включите Google Sheets API в Google Cloud.
2. Настройте ограничения API key согласно политике проекта и сети.
3. Убедитесь, что deployment имеет доступ к нужной таблице.

### Настройка Bitrix24

1. Создайте входящий webhook с правами на работу с контактами.
2. Укажите в `BITRIX_ALLOWED_HOST` точное имя хоста webhook.
3. Не сохраняйте webhook URL в Git и не выводите его в логи: он содержит секретный путь.

В исходной строке таблицы должны быть непустое имя в колонке **A** и корректный email в
колонке **B**.

Пример запроса:

```bash
curl -X POST https://sync.example.com/sync \
  -H 'X-Admin-Token: <long-random-secret>' \
  -H 'Content-Type: application/json' \
  -d '{"spreadsheet_id":"1abc...","row":2,"event_id":"sheet-change-42"}'
```

Оба HTTP-адаптера требуют HTTPS и проверяют настроенный host. Credentials нужно передавать
через secret manager или environment injection, а не хранить в репозитории.

## Конфигурация

| Переменная | Обязательна | Назначение |
|---|---:|---|
| `ADAPTER_MODE` | да | `demo` или `live` |
| `QUEUE_BACKEND` | production | `postgres` для надёжной очереди; `memory` только для локальных тестов |
| `POSTGRES_PASSWORD` | Docker | Пароль пользователя PostgreSQL |
| `DATABASE_URL` | PostgreSQL | Строка подключения к PostgreSQL |
| `REQUIRE_ADMIN_AUTH` | production | `true` — завершать запуск с ошибкой без настроенного токена |
| `SYNC_ADMIN_TOKEN` | production | Токен административных endpoints |
| `WORKER_LEASE_SECONDS` | нет | Длительность lease, по умолчанию `60` секунд |
| `WORKER_POLL_SECONDS` | нет | Интервал опроса пустой очереди, по умолчанию `1` секунда |
| `GOOGLE_API_KEY` | live | API key Google Sheets |
| `BITRIX_WEBHOOK_URL` | live | URL входящего webhook Bitrix24 |
| `BITRIX_ALLOWED_HOST` | live | Разрешённое имя хоста Bitrix24 |

`docker-compose.yml` по умолчанию запускает PostgreSQL, API и worker. Файл `.env.example`
настроен под эту топологию и должен быть изменён перед реальным запуском.

## API

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/healthz` | Проверка доступности базы и очереди |
| `GET` | `/metrics` | Метрики в формате Prometheus |
| `POST` | `/sync` | Добавить событие в очередь или дедуплицировать его (`202`) |
| `GET` | `/jobs/{job_id}` | Получить состояние задачи |
| `GET` | `/dlq` | Получить окончательно неудачные задачи |
| `POST` | `/dlq/{job_id}/replay` | Вернуть задачу из DLQ в очередь |
| `POST` | `/worker/tick` | Обработать одну задачу вручную; полезно для локального режима |

При `REQUIRE_ADMIN_AUTH=true` все endpoints, кроме `/healthz` и `/metrics`, требуют заголовок
`X-Admin-Token`. В production доступ к metrics также следует ограничить на уровне сети или
системы мониторинга.

Тело запроса для `/sync`:

```json
{"spreadsheet_id":"1abc...","row":2,"event_id":"sheet-change-42"}
```

Ключ идемпотентности: `sheet:{spreadsheet_id}:event:{event_id}`. Повторный такой же запрос
возвращает существующий `job_id` и `created: false`. Payload задач не включается в API-ответы,
чтобы не раскрывать персональные данные из таблицы.

Состояния задач: `pending`, `processing`, `done`, `dlq`. По умолчанию lease worker истекает
через 60 секунд; брошенная задача возвращается в `pending` при следующем claim.

## Повторы и DLQ

Timeout, HTTP 408, 429 и 5xx повторяются максимум пять раз. Задержка начинается с двух
секунд, удваивается между попытками, ограничена 300 секундами и получает jitter. Корректный
`Retry-After` учитывается с тем же ограничением. Ошибки валидации и остальные 4xx сразу
попадают в DLQ.

```bash
curl -H "X-Admin-Token: $TOKEN" http://localhost:8000/dlq
curl -X POST -H "X-Admin-Token: $TOKEN" \
  http://localhost:8000/dlq/<JOB_ID>/replay
```

Перед replay устраните причину ошибки: проверьте credentials, права доступа, лимиты и данные
строки. Подробный runbook находится в [`docs/runbook.md`](docs/runbook.md).

## Локальная разработка без PostgreSQL

Для быстрого изолированного demo используйте очередь в памяти и запустите API напрямую:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
ADAPTER_MODE=demo QUEUE_BACKEND=memory uvicorn app.main:app --reload
```

После этого вызовите `POST /sync`, а затем `POST /worker/tick`. Memory-режим теряет все задачи
при перезапуске и не должен использоваться в production.

## Тесты и структура репозитория

```bash
pytest -q
```

- `app/main.py` — FastAPI API и фабрика сервисов;
- `app/worker.py` — постоянно работающий production worker;
- `app/domain.py` — реализации очереди PostgreSQL и in-memory;
- `app/service.py` — orchestration и retry policy;
- `app/adapters.py` — demo-, Google Sheets- и Bitrix24-адаптеры;
- `migrations/001_initial.sql` — схема и индексы PostgreSQL;
- `docker-compose.yml` — топология PostgreSQL + API + worker;
- `docs/runbook.md` — инструкции по восстановлению;
- `docs/ADR-001-queue-and-delivery.md` — решение по доставке задач.

## Production-чеклист

- [ ] Заменить пример пароля базы и admin token на сгенерированные секреты.
- [ ] Использовать managed PostgreSQL или зашифрованный persistent volume с backup.
- [ ] Разместить API за TLS, network policy и внешним authentication gateway.
- [ ] Установить `ADAPTER_MODE=live`, ограничить Google API key и защитить Bitrix webhook.
- [ ] Настроить Prometheus-алерты для `sheetbridge_jobs{status="dlq"}` и
      `sheetbridge_failed_total`.
- [ ] Определить срок хранения, аудит и политику доступа к персональным данным.
- [ ] Запускать несколько worker-реплик только после проверки нагрузки на БД и лимитов upstream.
- [ ] Проверить восстановление из backup и процедуру replay.

## Лицензия

MIT — см. [LICENSE](LICENSE).
