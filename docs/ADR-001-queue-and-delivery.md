# ADR-001: очередь и доставка

**Статус:** accepted

Production использует PostgreSQL: enqueue защищён уникальным `idempotency_key`, а claim выполняется в транзакции через `FOR UPDATE SKIP LOCKED`. Lease возвращает незавершённую задачу в `pending` после сбоя worker. Доставка at-least-once; ключ события Sheets передаётся в CRM как идемпотентный ключ.

`InMemoryQueue` оставлен только для локального demo-режима и unit-тестов. Он не разделяет
данные между процессами и не должен использоваться в production. Схема находится в
`migrations/001_initial.sql`, а production worker — в `app/worker.py`.
