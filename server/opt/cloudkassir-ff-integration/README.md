# CloudKassir + FF Platform integration skeleton

Скелет сервиса для схемы:

1. Получаем и храним заказ, первый чек, события CloudKassir и данные склада.
2. После отгрузки FF Platform подтягиваем фактические товары и `kiz_codes`.
3. Формируем второй чек `полный расчет` с зачетом предоплаты и КИЗами.

По умолчанию сервис работает в `DRY_RUN=true`: payload чека сохраняется в БД, но не отправляется в CloudKassir.

`worker` автоматически делает две фоновые задачи:

- каждые `WORKER_POLL_INTERVAL_MS` проверяет оплаченные заказы с `skladDispatchId`, подтягивает FF Platform и создает второй чек;
- каждые `CLOUDKASSIR_POLL_INTERVAL_MS` опрашивает CloudPayments за последние `CLOUDKASSIR_POLL_LOOKBACK_MINUTES` минут и подтягивает детали последних известных локальных чеков по `cloudKassirId`.

Повторный FF sync не удаляет старые КИЗы: товары и коды помечаются `isActive=false`, а уже использованные КИЗы остаются в базе для аудита.

## Быстрый старт

```bash
cp .env.example .env
npm install
npm run db:generate
npm run db:push
npm run seed:example
npm run dev
```

В отдельном процессе запустить фоновые задачи:

```bash
npm run worker
```

Проверка:

```bash
curl http://localhost:3100/health
curl -X POST http://localhost:3100/api/orders/ORDER-330135/receipts/second
curl http://localhost:3100/api/orders/ORDER-330135
```

## Основные ручки

- `POST /api/orders` - создать или обновить заказ.
- `GET /api/orders/:externalId` - посмотреть заказ, товары, отгрузки, КИЗы и чеки.
- `POST /api/orders/:externalId/receipts/first` - собрать первый чек предоплаты.
- `POST /api/orders/:externalId/receipts/second` - собрать второй закрывающий чек.
- `POST /api/ff/sklad-dispatch/:id/sync?orderExternalId=ORDER-1` - подтянуть отгрузку и КИЗы из FF Platform.
- `GET /api/receipts` - список чеков.
- `POST /api/receipts/:id/refresh-status` - обновить статус чека в CloudKassir.
- `POST /webhooks/cloudkassir/receipt` - прием и импорт Receipt-уведомлений CloudKassir.
- `POST /api/cloudkassir/import/transactions` - импорт платежных транзакций CloudPayments по периоду.
- `POST /api/cloudkassir/import/receipts` - импорт чеков CloudKassir по известным `Id`.
- `GET /api/cloudkassir/transactions` - последние импортированные транзакции.
- `GET /api/cloudkassir/receipts` - последние импортированные чеки.
- `GET /api/cloudkassir/snapshot` - предупреждения касс и настройки Receipt-уведомления.
- `GET /api/admin/summary` - фискальная сводка: pending second, errors, stuck sending, KIZ-аудит.

## Импорт CloudKassir / CloudPayments

В CloudKassir API нет метода "выгрузить все чеки списком". Доступны:

- `POST /kkt/receipt/get` - получить чек по конкретному `Id`;
- `Receipt` webhook - получить чек сразу после выдачи;
- `POST /v2/payments/list` - выгрузить платежные транзакции CloudPayments по периоду.

Поэтому схема импорта такая:

1. Будущие чеки: включить в ЛК CloudKassir уведомление `Receipt` на URL `/webhooks/cloudkassir/receipt`.
2. Старые чеки: выгрузить/получить их `Id` из ЛК и запустить импорт по списку.
3. Платежи и "прочее": регулярно импортировать CloudPayments транзакции по периоду.

CLI-команды:

```bash
npm run import:cloudkassir:transactions -- \
  --from '2026-06-01T00:00:00+03:00' \
  --to '2026-06-29T00:00:00+03:00' \
  --timeZone MSK

npm run import:cloudkassir:receipts -- --ids receipt_id_1,receipt_id_2
npm run import:cloudkassir:receipts -- --file ./receipt-ids.txt
npm run import:cloudkassir:receipts-csv -- --file ./cloudkassir-export.csv
npm run cloudkassir:snapshot
```

Результаты хранятся подробно:

- `CloudPaymentTransaction` - нормализованные поля платежа + полный raw JSON;
- `CloudKassirReceipt` - нормализованные поля чека + полный raw JSON;
- `CloudKassirReceiptItem` - строки импортированного чека;
- `ApiEvent` - аудит webhook, snapshot и запусков импорта.

## Что нужно будет заполнить

```env
DRY_RUN=false
CLOUDKASSIR_PUBLIC_ID=...
CLOUDKASSIR_API_SECRET=...
CLOUDKASSIR_INN=...
CLOUDKASSIR_TAXATION_SYSTEM=...
CLOUDKASSIR_CALCULATION_PLACE=...
FF_PLATFORM_TOKEN=...
CLOUDKASSIR_POLL_ENABLED=true
CLOUDKASSIR_POLL_INTERVAL_MS=1800000
CLOUDKASSIR_POLL_LOOKBACK_MINUTES=60
CLOUDKASSIR_POLL_KNOWN_RECEIPT_LIMIT=200
CLOUDKASSIR_WEBHOOK_SECRET=...
ADMIN_SECRET=...
```

Если `CLOUDKASSIR_WEBHOOK_SECRET` задан, webhook принимает запросы только с `?secret=...` или заголовком `x-webhook-secret`.
Если `ADMIN_SECRET` задан, `/api/admin/*` принимает запросы только с `?secret=...` или заголовком `x-admin-secret`. В production админка закрыта, если не задан ни `ADMIN_SECRET`, ни `CLOUDKASSIR_WEBHOOK_SECRET`.

## Логика второго чека

- Берем заказ и связанные позиции.
- Синхронизируем склад по `skladDispatchId`.
- Для маркированных товаров проверяем, что КИЗов не меньше количества в заказе.
- Каждая единица с КИЗом становится отдельной строкой чека:
  - `Method: 4`
  - `Object: 33`
  - `RawMarkCode: код из FF Platform`
  - `GroupSeparator: "\u001D"`
  - `UnitCode: 0`
  - `ExpectedMarkItemStatus: 1`
- Оплата во втором чеке идет через `Amounts.AdvancePayment`, то есть как зачет предоплаты.

## Важные доработки перед продом

- Подключить реальные уведомления CloudKassir и проверку подписи/источника.
- Для webhook на проде обязательно задать `CLOUDKASSIR_WEBHOOK_SECRET` или закрыть endpoint на уровне gateway.
- Уточнить точные значения НДС и системы налогообложения.
- Решить, как сопоставлять заказ с `skladDispatchId`, если ID склада появляется не сразу.
- Добавить возвраты и коррекции.
- Перевести `DATABASE_URL` на Postgres, если будет несколько инстансов сервиса.
