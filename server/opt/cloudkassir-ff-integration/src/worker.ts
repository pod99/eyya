import { config } from "./config.js";
import { prisma } from "./db.js";
import { logger } from "./logger.js";
import { FfSyncService } from "./services/ff-sync-service.js";
import { FfScanService } from "./services/ff-scan-service.js";
import { CloudKassirImportService } from "./services/cloudkassir-import-service.js";
import { ReceiptService } from "./services/receipt-service.js";
import { CdekEnrichmentService } from "./services/cdek-enrichment-service.js";
import { OrderReconstructionService } from "./services/order-reconstruction-service.js";

const ffSyncService = new FfSyncService();
const ffScanService = new FfScanService();
const receiptService = new ReceiptService();
const cloudKassirImportService = new CloudKassirImportService();
const cdekEnrichmentService = new CdekEnrichmentService();
const orderReconstructionService = new OrderReconstructionService();
let ffScanRunning = false;
let cdekEnrichRunning = false;
let secondReceiptRunning = false;
let orderReconstructRunning = false;
const FINAL_RECEIPT_STATUSES = ["sent", "queued", "processed"];
const PROCESSED_RECEIPT_STATUSES = ["processed"];
const RETRY_LATER_STATUS = "retry_later";

async function secondReceiptTick() {
  if (secondReceiptRunning) {
    logger.warn("second receipt worker tick skipped: previous tick is still running");
    return;
  }

  secondReceiptRunning = true;
  try {
    const blockingReceiptStatuses = config.DRY_RUN ? ["dry_run", ...FINAL_RECEIPT_STATUSES] : FINAL_RECEIPT_STATUSES;
    const retryLaterCutoff = receiptRetryCutoff();
    const fullyReturnedInvoiceIds = await findFullyReturnedInvoiceIds();
    const orders = await prisma.order.findMany({
      where: {
        paymentStatus: "paid",
        skladDispatchId: { not: null },
        ...(fullyReturnedInvoiceIds.length > 0 ? { externalId: { notIn: fullyReturnedInvoiceIds } } : {}),
        receipts: {
          none: {
            kind: "second_fullpay",
            OR: [
              { status: { in: blockingReceiptStatuses } },
              { status: RETRY_LATER_STATUS, updatedAt: { gt: retryLaterCutoff } }
            ]
          }
        }
      },
      take: 20
    });
    const ordersWithDispatch = orders.filter(
      (order): order is typeof order & { skladDispatchId: number } => order.skladDispatchId !== null
    );
    for (const order of ordersWithDispatch) {
      try {
        await ffSyncService.syncSkladDispatch(order.skladDispatchId, order.externalId);
        await receiptService.createSecondFullPayReceipt(order.externalId);
        logger.info({ orderExternalId: order.externalId }, "second receipt handled");
      } catch (error) {
        const retryLaterMarked = await markReceiptRetryLater({
          orderId: order.id,
          kind: "second_fullpay",
          requestId: `second-fullpay:${order.externalId}:v1`,
          invoiceId: order.externalId,
          error
        });
        logger[retryLaterMarked ? "warn" : "error"](
          { error: errorDetails(error), orderExternalId: order.externalId, retryLaterMarked },
          retryLaterMarked ? "second receipt postponed until source data is ready" : "second receipt failed"
        );
      }
    }

    await createPendingSecondFullPayReturnReceipts(blockingReceiptStatuses);
  } finally {
    secondReceiptRunning = false;
  }
}

async function findFullyReturnedInvoiceIds(): Promise<string[]> {
  const receipts = await prisma.cloudKassirReceipt.findMany({
    where: {
      type: "IncomeReturn",
      OR: [{ status: "Processed" }, { status: "processed" }],
      invoiceId: { not: null }
    },
    select: {
      invoiceId: true,
      amountKop: true,
      items: {
        select: { amountKop: true }
      }
    },
    orderBy: [{ dateTime: "asc" }, { importedAt: "asc" }]
  });

  const returnedByInvoiceId = new Map<string, number>();
  for (const receipt of receipts) {
    if (!receipt.invoiceId) {
      continue;
    }

    returnedByInvoiceId.set(
      receipt.invoiceId,
      (returnedByInvoiceId.get(receipt.invoiceId) ?? 0) + receiptAmountKop(receipt)
    );
  }

  const orders = await prisma.order.findMany({
    where: { externalId: { in: [...returnedByInvoiceId.keys()] } },
    select: { externalId: true, amountKop: true }
  });

  return orders
    .filter((order) => (returnedByInvoiceId.get(order.externalId) ?? 0) >= order.amountKop - 1)
    .map((order) => order.externalId);
}

async function createPendingSecondFullPayReturnReceipts(blockingReceiptStatuses: string[]) {
  const returnReceipts = await prisma.cloudKassirReceipt.findMany({
    where: {
      type: "IncomeReturn",
      OR: [{ status: "Processed" }, { status: "processed" }],
      invoiceId: { not: null }
    },
    select: {
      cloudKassirId: true,
      invoiceId: true,
      dateTime: true,
      importedAt: true
    },
    orderBy: [{ dateTime: "asc" }, { importedAt: "asc" }]
  });

  const invoiceIds = [
    ...new Set(
      returnReceipts
        .map((receipt) => receipt.invoiceId)
        .filter((invoiceId): invoiceId is string => Boolean(invoiceId))
    )
  ];
  if (invoiceIds.length === 0) {
    return;
  }

  const secondFullPayStatuses = config.DRY_RUN ? ["dry_run", ...PROCESSED_RECEIPT_STATUSES] : PROCESSED_RECEIPT_STATUSES;
  const orders = await prisma.order.findMany({
    where: { externalId: { in: invoiceIds } },
    select: {
      id: true,
      externalId: true,
      receipts: {
        select: {
          kind: true,
          status: true,
          requestId: true,
          cloudKassirId: true,
          createdAt: true,
          updatedAt: true
        },
        orderBy: { createdAt: "asc" }
      }
    }
  });
  const ordersByExternalId = new Map(orders.map((order) => [order.externalId, order]));
  const importedSecondReceiptCache = new Map<string, { dateTime: string | null; importedAt: Date } | null>();
  const retryLaterCutoff = receiptRetryCutoff();

  for (const returnReceipt of returnReceipts) {
    if (!returnReceipt.invoiceId) {
      continue;
    }

    const order = ordersByExternalId.get(returnReceipt.invoiceId);
    if (!order) {
      continue;
    }

    const secondFullPayReceipt = order.receipts.find(
      (receipt) =>
        receipt.kind === "second_fullpay" &&
        secondFullPayStatuses.includes(receipt.status) &&
        Boolean(receipt.cloudKassirId)
    );
    if (!secondFullPayReceipt?.cloudKassirId) {
      continue;
    }

    const importedSecondFullPayReceipt = await loadImportedSecondFullPayReceipt(
      importedSecondReceiptCache,
      secondFullPayReceipt.cloudKassirId
    );

    if (!isReturnAfterSecondFullPay(returnReceipt, secondFullPayReceipt, importedSecondFullPayReceipt ?? null)) {
      continue;
    }

    const requestId = `second-fullpay-return:${order.externalId}:${returnReceipt.cloudKassirId}:v1`;
    const hasBlockingReturnReceipt = order.receipts.some(
      (receipt) =>
        receipt.requestId === requestId &&
        (blockingReceiptStatuses.includes(receipt.status) ||
          (receipt.status === RETRY_LATER_STATUS && receipt.updatedAt > retryLaterCutoff))
    );
    if (hasBlockingReturnReceipt) {
      continue;
    }

    try {
      await receiptService.createSecondFullPayReturnReceipt(order.externalId, returnReceipt.cloudKassirId);
      logger.info(
        { orderExternalId: order.externalId, returnCloudKassirId: returnReceipt.cloudKassirId },
        "second full-pay return receipt handled"
      );
    } catch (error) {
      const retryLaterMarked = await markReceiptRetryLater({
        orderId: order.id,
        kind: "second_fullpay_return",
        requestId,
        invoiceId: order.externalId,
        error
      });
      logger[retryLaterMarked ? "warn" : "error"](
        { error: errorDetails(error), orderExternalId: order.externalId, returnCloudKassirId: returnReceipt.cloudKassirId, retryLaterMarked },
        retryLaterMarked
          ? "second full-pay return receipt postponed until source data is ready"
          : "second full-pay return receipt failed"
      );
    }
  }
}

async function loadImportedSecondFullPayReceipt(
  cache: Map<string, { dateTime: string | null; importedAt: Date } | null>,
  cloudKassirId: string
): Promise<{ dateTime: string | null; importedAt: Date } | null> {
  if (!cache.has(cloudKassirId)) {
    const importedReceipt = await prisma.cloudKassirReceipt.findUnique({
      where: { cloudKassirId },
      select: { dateTime: true, importedAt: true }
    });
    cache.set(cloudKassirId, importedReceipt);
  }

  return cache.get(cloudKassirId) ?? null;
}

function receiptAmountKop(receipt: { amountKop: number | null; items: Array<{ amountKop: number | null }> }): number {
  if (receipt.amountKop !== null) {
    return receipt.amountKop;
  }

  return receipt.items.reduce((sum, item) => sum + (item.amountKop ?? 0), 0);
}

function isReturnAfterSecondFullPay(
  returnReceipt: { dateTime: string | null; importedAt: Date },
  secondFullPayReceipt: { createdAt: Date; updatedAt: Date },
  importedSecondFullPayReceipt: { dateTime: string | null; importedAt: Date } | null
): boolean {
  const returnTime = parseReceiptDateTime(returnReceipt.dateTime) ?? returnReceipt.importedAt.getTime();
  const secondTime =
    parseReceiptDateTime(importedSecondFullPayReceipt?.dateTime ?? null) ??
    importedSecondFullPayReceipt?.importedAt.getTime() ??
    secondFullPayReceipt.updatedAt.getTime() ??
    secondFullPayReceipt.createdAt.getTime();

  return returnTime > secondTime;
}

function parseReceiptDateTime(value: string | null): number | null {
  if (!value) {
    return null;
  }

  const parsed = new Date(value.replace(" ", "T"));
  return Number.isNaN(parsed.getTime()) ? null : parsed.getTime();
}

async function ffScanTick() {
  if (!config.FF_SCAN_ENABLED) {
    logger.info("FF scanner is disabled");
    return;
  }

  if (!config.FF_PLATFORM_TOKEN) {
    logger.warn("FF scanner skipped: FF_PLATFORM_TOKEN is not configured");
    return;
  }

  if (ffScanRunning) {
    logger.warn("FF scanner tick skipped: previous tick is still running");
    return;
  }

  ffScanRunning = true;
  try {
    const result = await ffScanService.scanNextBatch();
    logger.info(
      {
        mode: result.mode,
        from: result.mode === "cdek_period" ? result.from : undefined,
        to: result.mode === "cdek_period" ? result.to : undefined,
        statuses: result.mode === "cdek_period" ? result.statuses : undefined,
        listed: result.mode === "cdek_period" ? result.listed : undefined,
        skipped: result.mode === "cdek_period" ? result.skipped : undefined,
        startId: result.mode === "id_range" ? result.startId : undefined,
        endId: result.mode === "id_range" ? result.endId : undefined,
        scanned: result.scanned,
        missing: result.missing,
        saved: result.saved,
        errors: result.errors,
        nextSkladDispatchId: result.cursor.nextSkladDispatchId,
        lastFoundId: result.cursor.lastFoundId
      },
      "FF scan completed"
    );
  } finally {
    ffScanRunning = false;
  }
}

async function orderReconstructTick() {
  if (!config.ORDER_RECONSTRUCT_ENABLED) {
    logger.info("order reconstruction is disabled");
    return;
  }

  if (orderReconstructRunning) {
    logger.warn("order reconstruction tick skipped: previous tick is still running");
    return;
  }

  orderReconstructRunning = true;
  try {
    const result = await orderReconstructionService.reconstructPendingOrders({
      take: config.ORDER_RECONSTRUCT_BATCH_SIZE,
      receiptDateFrom: config.ORDER_RECONSTRUCT_DATE_FROM || undefined,
      receiptDateTo: config.ORDER_RECONSTRUCT_DATE_TO || undefined
    });
    logger.info(result, "order reconstruction completed");
  } finally {
    orderReconstructRunning = false;
  }
}

async function cloudKassirPollTick() {
  if (!config.CLOUDKASSIR_POLL_ENABLED) {
    logger.info("CloudKassir polling is disabled");
    return;
  }

  if (!config.CLOUDKASSIR_PUBLIC_ID || !config.CLOUDKASSIR_API_SECRET) {
    logger.warn("CloudKassir polling skipped: API credentials are not configured");
    return;
  }

  const to = new Date();
  const from = new Date(to.getTime() - config.CLOUDKASSIR_POLL_LOOKBACK_MINUTES * 60_000);
  let transactions: Awaited<ReturnType<CloudKassirImportService["importTransactionsByPeriod"]>> | null = null;
  let receiptList: Awaited<ReturnType<CloudKassirImportService["importReceiptsByFiscalPeriod"]>> | null = null;
  let receipts: Awaited<ReturnType<CloudKassirImportService["importReceiptsByIds"]>> | { requested: number; imported: number } | null =
    null;

  try {
    transactions = await cloudKassirImportService.importTransactionsByPeriod({
      from: from.toISOString(),
      to: to.toISOString(),
      timeZone: config.CLOUDKASSIR_POLL_TIME_ZONE,
      maxPages: config.CLOUDKASSIR_POLL_MAX_PAGES
    });
  } catch (error) {
    logger.error({ error: errorDetails(error) }, "CloudKassir transactions import failed");
  }

  try {
    receiptList = await cloudKassirImportService.importReceiptsByFiscalPeriod({
      from: from.toISOString(),
      to: to.toISOString(),
      limit: config.CLOUDKASSIR_POLL_RECEIPT_PAGE_LIMIT,
      maxPages: config.CLOUDKASSIR_POLL_MAX_PAGES
    });
  } catch (error) {
    logger.error({ error: errorDetails(error) }, "CloudKassir fiscal receipt list import failed");
  }

  try {
    const knownReceiptIds = await prisma.fiscalReceipt.findMany({
      where: {
        cloudKassirId: { not: null },
        status: { in: ["sent", "queued"] }
      },
      select: { cloudKassirId: true },
      orderBy: { updatedAt: "asc" },
      take: config.CLOUDKASSIR_POLL_KNOWN_RECEIPT_LIMIT
    });
    const uniqueReceiptIds = [
      ...new Set(
        knownReceiptIds
          .map((receipt) => receipt.cloudKassirId)
          .filter((cloudKassirId): cloudKassirId is string => Boolean(cloudKassirId))
      )
    ];
    receipts =
      uniqueReceiptIds.length > 0
        ? await cloudKassirImportService.importReceiptsByIds(uniqueReceiptIds)
        : { requested: 0, imported: 0 };
  } catch (error) {
    logger.error({ error: errorDetails(error) }, "CloudKassir receipt status import failed");
  }

  logger.info(
    {
      transactionsImported: transactions?.imported ?? 0,
      transactionWindows: transactions?.pages.length ?? 0,
      receiptListImported: receiptList?.imported ?? 0,
      receiptListPages: receiptList?.pages.length ?? 0,
      knownReceiptsRequested: receipts?.requested ?? 0,
      knownReceiptsImported: receipts?.imported ?? 0
    },
    "CloudKassir poll completed"
  );

  await orderReconstructTick();
}

async function cdekEnrichTick() {
  if (!config.CDEK_ENRICH_ENABLED) {
    logger.info("CDEK enrichment is disabled");
    return;
  }

  if (!config.CDEK_CLIENT_ID || !config.CDEK_CLIENT_SECRET) {
    logger.warn("CDEK enrichment skipped: credentials are not configured");
    return;
  }

  if (cdekEnrichRunning) {
    logger.warn("CDEK enrichment tick skipped: previous tick is still running");
    return;
  }

  cdekEnrichRunning = true;
  try {
    const result = await cdekEnrichmentService.enrichPendingShipments();
    logger.info(result, "CDEK enrichment completed");
    await orderReconstructTick();
  } finally {
    cdekEnrichRunning = false;
  }
}

secondReceiptTick().catch((error) => {
  logger.error({ error: errorDetails(error) }, "initial second receipt worker tick failed");
});

if (config.FF_SCAN_ENABLED) {
  ffScanTick().catch((error) => {
    logger.error({ error: errorDetails(error) }, "initial FF scan failed");
  });
}

if (config.CLOUDKASSIR_POLL_ENABLED) {
  cloudKassirPollTick().catch((error) => {
    logger.error({ error: errorDetails(error) }, "initial CloudKassir poll failed");
  });
}

if (config.CDEK_ENRICH_ENABLED) {
  cdekEnrichTick().catch((error) => {
    logger.error({ error: errorDetails(error) }, "initial CDEK enrichment failed");
  });
}

if (config.ORDER_RECONSTRUCT_ENABLED) {
  orderReconstructTick().catch((error) => {
    logger.error({ error: errorDetails(error) }, "initial order reconstruction failed");
  });
}

setInterval(() => {
  secondReceiptTick().catch((error) => {
    logger.error({ error: errorDetails(error) }, "second receipt worker tick failed");
  });
}, config.WORKER_POLL_INTERVAL_MS);

if (config.FF_SCAN_ENABLED) {
  setInterval(() => {
    ffScanTick().catch((error) => {
      logger.error({ error: errorDetails(error) }, "FF scan failed");
    });
  }, config.FF_SCAN_INTERVAL_MS);
}

if (config.CLOUDKASSIR_POLL_ENABLED) {
  setInterval(() => {
    cloudKassirPollTick().catch((error) => {
      logger.error({ error: errorDetails(error) }, "CloudKassir poll failed");
    });
  }, config.CLOUDKASSIR_POLL_INTERVAL_MS);
}

if (config.CDEK_ENRICH_ENABLED) {
  setInterval(() => {
    cdekEnrichTick().catch((error) => {
      logger.error({ error: errorDetails(error) }, "CDEK enrichment failed");
    });
  }, config.CDEK_ENRICH_INTERVAL_MS);
}

if (config.ORDER_RECONSTRUCT_ENABLED) {
  setInterval(() => {
    orderReconstructTick().catch((error) => {
      logger.error({ error: errorDetails(error) }, "order reconstruction failed");
    });
  }, config.WORKER_POLL_INTERVAL_MS);
}


function receiptRetryCutoff(): Date {
  return new Date(Date.now() - config.SECOND_RECEIPT_RETRY_LATER_MINUTES * 60_000);
}

async function markReceiptRetryLater(input: {
  orderId: number;
  kind: string;
  requestId: string;
  invoiceId: string;
  error: unknown;
}): Promise<boolean> {
  if (!isRetryableReceiptPreconditionError(input.error)) {
    return false;
  }

  const message = errorMessage(input.error);
  const payloadJson = JSON.stringify({
    blockedBy: "worker_precondition",
    retryAfterMinutes: config.SECOND_RECEIPT_RETRY_LATER_MINUTES
  });
  const existing = await prisma.fiscalReceipt.findUnique({ where: { requestId: input.requestId } });
  if (existing && FINAL_RECEIPT_STATUSES.includes(existing.status)) {
    return true;
  }

  if (existing) {
    await prisma.fiscalReceipt.update({
      where: { requestId: input.requestId },
      data: {
        status: RETRY_LATER_STATUS,
        payloadJson: existing.payloadJson || payloadJson,
        errorMessage: message
      }
    });
    return true;
  }

  await prisma.fiscalReceipt.create({
    data: {
      orderId: input.orderId,
      kind: input.kind,
      requestId: input.requestId,
      invoiceId: input.invoiceId,
      status: RETRY_LATER_STATUS,
      payloadJson,
      errorMessage: message
    }
  });
  return true;
}

function isRetryableReceiptPreconditionError(error: unknown): boolean {
  const message = errorMessage(error);
  return [
    "Not enough KIZ codes",
    "No completed FF Platform shipment found",
    "Active KIZ codes are not ready",
    "Active KIZ codes are not linked",
    "Order was fully returned before second full-pay receipt",
    "Cannot create full-pay return receipt before second full-pay receipt",
    "Cannot match return item",
    "Full-pay return amount mismatch",
    "Marked return amount mismatch",
    "Not enough marked lines",
    "Return receipt"
  ].some((fragment) => message.includes(fragment));
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}

function errorDetails(error: unknown): unknown {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: error.message,
      stack: error.stack
    };
  }

  return error;
}
