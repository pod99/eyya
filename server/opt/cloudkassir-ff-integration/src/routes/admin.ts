import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { Prisma } from "@prisma/client";
import { z } from "zod";
import { config } from "../config.js";
import { prisma } from "../db.js";
import { FfSyncService } from "../services/ff-sync-service.js";
import { ReceiptService } from "../services/receipt-service.js";
import { kopToRub, rubToKop } from "../utils/money.js";
import { basicAuthChallenge, isAdminAuthorized } from "./auth.js";

const FINAL_RECEIPT_STATUSES = ["sent", "queued", "processed"];
const PROCESSED_RECEIPT_STATUSES = ["processed"];
const PROBLEM_RECEIPT_STATUSES = ["error", "sending"];
const MAX_TAKE = 200;

const listQuerySchema = z.object({
  search: z.string().trim().optional(),
  status: z.string().trim().optional(),
  kind: z.string().trim().optional(),
  eventType: z.string().trim().optional(),
  problem: z.string().trim().optional(),
  take: z.coerce.number().int().positive().max(MAX_TAKE).default(80)
});
type ListQuery = z.infer<typeof listQuerySchema>;

const exportTypeSchema = z.enum([
  "orders",
  "fiscal-receipts",
  "imported-receipts",
  "transactions",
  "webhook-events",
  "mark-codes",
  "shipments",
  "tracks",
  "returns",
  "cdek"
]);
type ExportType = z.infer<typeof exportTypeSchema>;
type ExportRow = Record<string, unknown>;

export async function registerAdminRoutes(app: FastifyInstance) {
  const ffSyncService = new FfSyncService();
  const receiptService = new ReceiptService();

  app.get("/api/admin/summary", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const sendingStuckBefore = new Date(Date.now() - config.FISCAL_RECEIPT_SENDING_TIMEOUT_MS);
    const dayAgo = new Date(Date.now() - 24 * 60 * 60 * 1000);
    const fullyReturnedInvoiceIds = await findFullyReturnedOrderExternalIds();
    const pendingSecondWhere: Prisma.OrderWhereInput = {
      paymentStatus: "paid",
      skladDispatchId: { not: null },
      ...(fullyReturnedInvoiceIds.length > 0 ? { externalId: { notIn: fullyReturnedInvoiceIds } } : {}),
      receipts: {
        none: {
          kind: "second_fullpay",
          status: { in: FINAL_RECEIPT_STATUSES }
        }
      }
    };

    const [
      totalOrders,
      paidOrders,
      pendingSecond,
      incomeReturns,
      pendingSecondReturns,
      firstReceiptCounts,
      secondReceiptCounts,
      fiscalReceiptCounts,
      importedReceipts,
      errorReceipts,
      sendingStuck,
      unusedMarksAfterSent,
      unlinkedActiveMarks,
      activeMarks,
      webhookEvents24h,
      transactions24h,
      shipments,
      recentErrors
    ] = await Promise.all([
      prisma.order.count(),
      prisma.order.count({ where: { paymentStatus: "paid" } }),
      prisma.order.count({ where: pendingSecondWhere }),
      prisma.cloudKassirReceipt.count({ where: { type: "IncomeReturn" } }),
      countPendingSecondFullPayReturns(),
      countFiscalReceiptsByStatus("first_prepayment"),
      countFiscalReceiptsByStatus("second_fullpay"),
      prisma.fiscalReceipt.groupBy({
        by: ["status"],
        _count: { _all: true }
      }),
      prisma.cloudKassirReceipt.count(),
      prisma.fiscalReceipt.count({
        where: { status: "error" }
      }),
      prisma.fiscalReceipt.count({
        where: {
          status: "sending",
          updatedAt: { lt: sendingStuckBefore }
        }
      }),
      prisma.markCode.count({
        where: {
          isActive: true,
          usedInSecondReceipt: false,
          orderItem: {
            order: {
              receipts: {
                some: {
                  kind: "second_fullpay",
                  status: { in: FINAL_RECEIPT_STATUSES }
                }
              }
            }
          }
        }
      }),
      prisma.markCode.count({
        where: {
          isActive: true,
          orderItemId: null
        }
      }),
      prisma.markCode.count({
        where: { isActive: true }
      }),
      prisma.apiEvent.count({
        where: { createdAt: { gte: dayAgo } }
      }),
      prisma.cloudPaymentTransaction.count({
        where: { importedAt: { gte: dayAgo } }
      }),
      prisma.shipment.count(),
      prisma.fiscalReceipt.findMany({
        where: { status: { in: PROBLEM_RECEIPT_STATUSES } },
        orderBy: { updatedAt: "desc" },
        take: 20,
        select: {
          id: true,
          orderId: true,
          kind: true,
          status: true,
          requestId: true,
          invoiceId: true,
          cloudKassirId: true,
          errorMessage: true,
          updatedAt: true,
          order: {
            select: { externalId: true }
          }
        }
      })
    ]);

    return {
      generatedAt: new Date().toISOString(),
      dryRun: config.DRY_RUN,
      sendingTimeoutMs: config.FISCAL_RECEIPT_SENDING_TIMEOUT_MS,
      counts: {
        orders_total: totalOrders,
        orders_paid: paidOrders,
        pending_second: pendingSecond,
        return_receipts: incomeReturns,
        pending_second_returns: pendingSecondReturns,
        first_receipts: sumStatusCounts(firstReceiptCounts),
        second_receipts: sumStatusCounts(secondReceiptCounts),
        imported_receipts: importedReceipts,
        error: errorReceipts,
        sending_stuck: sendingStuck,
        unused_marks_after_sent: unusedMarksAfterSent,
        unlinked_active_marks: unlinkedActiveMarks,
        active_marks: activeMarks,
        webhook_events_24h: webhookEvents24h,
        transactions_24h: transactions24h,
        shipments
      },
      receiptStatuses: {
        all: mapGroupCount(fiscalReceiptCounts),
        first: mapGroupCount(firstReceiptCounts),
        second: mapGroupCount(secondReceiptCounts)
      },
      recentErrors
    };
  });

  app.get("/api/admin/orders", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const where: Prisma.OrderWhereInput = {};

    if (query.status === "paid") {
      where.paymentStatus = "paid";
    }
    if (query.status === "pending_second") {
      const fullyReturnedInvoiceIds = await findFullyReturnedOrderExternalIds();
      where.paymentStatus = "paid";
      where.skladDispatchId = { not: null };
      if (fullyReturnedInvoiceIds.length > 0) {
        where.externalId = { notIn: fullyReturnedInvoiceIds };
      }
      where.receipts = {
        none: {
          kind: "second_fullpay",
          status: { in: FINAL_RECEIPT_STATUSES }
        }
      };
    }
    if (query.status === "error") {
      where.receipts = { some: { status: "error" } };
    }
    if (query.status === "without_shipment") {
      where.skladDispatchId = null;
    }
    if (query.search) {
      where.OR = orderSearch(query.search);
    }

    const orders = await prisma.order.findMany({
      where,
      orderBy: { updatedAt: "desc" },
      take: query.take,
      include: {
        items: {
          where: { isActive: true },
          include: {
            markCodes: {
              where: { isActive: true },
              select: {
                id: true,
                usedInSecondReceipt: true,
                orderItemId: true
              }
            }
          }
        },
        shipments: {
          orderBy: { updatedAt: "desc" },
          take: 1,
          include: {
            products: {
              where: { isActive: true },
              select: {
                id: true,
                clientCode: true,
                name: true,
                factQty: true,
                markCodes: {
                  where: { isActive: true },
                  select: {
                    id: true,
                    usedInSecondReceipt: true,
                    orderItemId: true
                  }
                }
              }
            }
          }
        },
        receipts: {
          orderBy: { updatedAt: "desc" }
        }
      }
    });

    return {
      rows: orders.map((order) => {
        const firstReceipt = latestByKind(order.receipts, "first_prepayment");
        const secondReceipt = latestByKind(order.receipts, "second_fullpay");
        const requiredMarks = order.items.reduce((sum, item) => sum + (item.isMarked ? item.quantity : 0), 0);
        const linkedMarks = order.items.reduce(
          (sum, item) => sum + item.markCodes.filter((markCode) => markCode.orderItemId === item.id).length,
          0
        );
        const usedMarks = order.items.reduce(
          (sum, item) => sum + item.markCodes.filter((markCode) => markCode.usedInSecondReceipt).length,
          0
        );
        const latestShipment = order.shipments[0] ?? null;

        return {
          id: order.id,
          externalId: order.externalId,
          accountId: order.accountId,
          customerEmail: order.customerEmail,
          customerPhone: order.customerPhone,
          amountKop: order.amountKop,
          currency: order.currency,
          paymentStatus: order.paymentStatus,
          skladDispatchId: order.skladDispatchId,
          deliveryTrackNumber: order.deliveryTrackNumber,
          itemsCount: order.items.length,
          markedRequired: requiredMarks,
          linkedMarks,
          usedMarks,
          firstReceipt,
          secondReceipt,
          latestShipment,
          createdAt: order.createdAt,
          updatedAt: order.updatedAt
        };
      })
    };
  });

  app.get("/api/admin/receipts", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const fiscalWhere: Prisma.FiscalReceiptWhereInput = {};
    const importedWhere: Prisma.CloudKassirReceiptWhereInput = {};

    if (query.status) {
      fiscalWhere.status = query.status;
      importedWhere.status = query.status;
    }
    if (query.kind) {
      fiscalWhere.kind = query.kind;
    }
    if (query.search) {
      fiscalWhere.OR = fiscalReceiptSearch(query.search);
      importedWhere.OR = importedReceiptSearch(query.search);
    }

    const [fiscalReceipts, importedReceipts] = await Promise.all([
      prisma.fiscalReceipt.findMany({
        where: fiscalWhere,
        include: {
          order: {
            select: { externalId: true }
          }
        },
        orderBy: { updatedAt: "desc" },
        take: query.take
      }),
      prisma.cloudKassirReceipt.findMany({
        where: importedWhere,
        include: {
          items: {
            select: {
              id: true,
              label: true,
              amountKop: true,
              method: true,
              object: true,
              rawMarkCode: true
            }
          }
        },
        orderBy: { importedAt: "desc" },
        take: query.take
      })
    ]);

    return { fiscalReceipts, importedReceipts };
  });

  app.get("/api/admin/returns", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const rows = await buildReturnReceiptRows(query);
    return { rows };
  });

  app.post("/api/admin/returns/:cloudKassirId/second-fullpay-return", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const { cloudKassirId } = request.params as { cloudKassirId: string };
    const returnReceipt = await prisma.cloudKassirReceipt.findUnique({
      where: { cloudKassirId },
      select: { invoiceId: true, type: true }
    });
    if (!returnReceipt) {
      return reply.code(404).send({ error: "Return receipt not found" });
    }
    if (returnReceipt.type !== "IncomeReturn") {
      return reply.code(400).send({ error: "Receipt is not IncomeReturn" });
    }
    if (!returnReceipt.invoiceId) {
      return reply.code(400).send({ error: "Return receipt has no invoiceId" });
    }

    return receiptService.createSecondFullPayReturnReceipt(returnReceipt.invoiceId, cloudKassirId);
  });

  app.get("/api/admin/webhook-events", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const where: Prisma.ApiEventWhereInput = {};

    if (query.eventType) {
      where.eventType = query.eventType;
    }
    if (query.search) {
      where.OR = [
        { externalId: { contains: query.search } },
        { eventType: { contains: query.search } },
        { payloadJson: { contains: query.search } }
      ];
    }

    const rows = await prisma.apiEvent.findMany({
      where,
      orderBy: { createdAt: "desc" },
      take: query.take
    });

    return { rows };
  });

  app.get("/api/admin/transactions", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const where: Prisma.CloudPaymentTransactionWhereInput = {};

    if (query.status) {
      where.status = query.status;
    }
    if (query.search) {
      where.OR = [
        { transactionId: { contains: query.search } },
        { invoiceId: { contains: query.search } },
        { accountId: { contains: query.search } },
        { email: { contains: query.search } },
        { rawJson: { contains: query.search } }
      ];
    }

    const rows = await prisma.cloudPaymentTransaction.findMany({
      where,
      orderBy: { updatedAt: "desc" },
      take: query.take
    });

    return { rows };
  });

  app.get("/api/admin/mark-codes", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const where: Prisma.MarkCodeWhereInput = {};

    if (query.problem === "unlinked") {
      where.isActive = true;
      where.orderItemId = null;
    }
    if (query.problem === "unused_after_sent") {
      where.isActive = true;
      where.usedInSecondReceipt = false;
      where.orderItem = {
        order: {
          receipts: {
            some: {
              kind: "second_fullpay",
              status: { in: FINAL_RECEIPT_STATUSES }
            }
          }
        }
      };
    }
    if (query.problem === "unused") {
      where.isActive = true;
      where.usedInSecondReceipt = false;
    }
    if (query.problem === "used") {
      where.usedInSecondReceipt = true;
    }
    if (query.search) {
      where.OR = markCodeSearch(query.search);
    }

    const rows = await prisma.markCode.findMany({
      where,
      include: {
        orderItem: {
          select: {
            id: true,
            clientCode: true,
            name: true,
            isActive: true,
            order: {
              select: { externalId: true }
            }
          }
        },
        shipmentProduct: {
          select: {
            id: true,
            ffProductId: true,
            clientCode: true,
            name: true,
            factQty: true,
            shipment: {
              select: {
                skladDispatchId: true,
                status: true,
                trackNumber: true,
                order: {
                  select: { externalId: true }
                }
              }
            }
          }
        }
      },
      orderBy: { updatedAt: "desc" },
      take: query.take
    });

    return { rows };
  });

  app.get("/api/admin/shipments", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    const where: Prisma.ShipmentWhereInput = {};

    if (query.status) {
      where.status = query.status;
    }
    if (query.search) {
      where.OR = shipmentSearch(query.search);
    }

    const rows = await prisma.shipment.findMany({
      where,
      include: {
        order: {
          select: { externalId: true }
        },
        products: {
          where: { isActive: true },
          include: {
            markCodes: {
              where: { isActive: true },
              select: {
                id: true,
                orderItemId: true,
                usedInSecondReceipt: true
              }
            }
          }
        }
      },
      orderBy: { updatedAt: "desc" },
      take: query.take
    });

    return { rows };
  });

  app.get("/api/admin/cdek", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const query = listQuerySchema.parse(request.query);
    return buildCdekReport(query);
  });

  app.get("/api/admin/settings", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const ffScanCursor = await prisma.ffScanCursor.findUnique({
      where: { name: "default" }
    });
    const webhookEvents = ["check", "pay", "fail", "confirm", "refund", "receipt", "sbptoken", "kkt", "recurrent", "cancel"];

    return {
      dryRun: config.DRY_RUN,
      nodeEnv: config.NODE_ENV,
      inn: config.CLOUDKASSIR_INN,
      publicId: config.CLOUDKASSIR_PUBLIC_ID,
      cloudKassirApiConfigured: Boolean(config.CLOUDKASSIR_API_SECRET),
      ffPlatformConfigured: Boolean(config.FF_PLATFORM_TOKEN),
      taxationSystem: config.CLOUDKASSIR_TAXATION_SYSTEM,
      calculationPlace: config.CLOUDKASSIR_CALCULATION_PLACE,
      workerPollIntervalMs: config.WORKER_POLL_INTERVAL_MS,
      externalApiTimeoutMs: config.EXTERNAL_API_TIMEOUT_MS,
      cloudKassirPollEnabled: config.CLOUDKASSIR_POLL_ENABLED,
      cloudKassirPollIntervalMs: config.CLOUDKASSIR_POLL_INTERVAL_MS,
      cloudKassirPollLookbackMinutes: config.CLOUDKASSIR_POLL_LOOKBACK_MINUTES,
      cdek: {
        configured: Boolean(config.CDEK_CLIENT_ID && config.CDEK_CLIENT_SECRET),
        enrichEnabled: config.CDEK_ENRICH_ENABLED,
        enrichIntervalMs: config.CDEK_ENRICH_INTERVAL_MS,
        enrichBatchSize: config.CDEK_ENRICH_BATCH_SIZE
      },
      ffScan: {
        enabled: config.FF_SCAN_ENABLED,
        mode: config.FF_SCAN_MODE,
        startId: config.FF_SCAN_START_ID,
        batchSize: config.FF_SCAN_BATCH_SIZE,
        concurrency: config.FF_SCAN_CONCURRENCY,
        intervalMs: config.FF_SCAN_INTERVAL_MS,
        lookbackDays: config.FF_SCAN_LOOKBACK_DAYS,
        from: config.FF_SCAN_FROM,
        to: config.FF_SCAN_TO,
        statuses: config.FF_SCAN_STATUSES,
        cursor: ffScanCursor
      },
      cloudKassirWebhookSecretConfigured: Boolean(config.CLOUDKASSIR_WEBHOOK_SECRET),
      webhookEvents,
      webhookUrlPaths: webhookEvents.map((event) => ({
        event,
        path: `/webhooks/cloudkassir/${event}/<secret>`
      }))
    };
  });

  app.post("/api/admin/webhook-urls/reveal", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const webhookEvents = ["check", "pay", "fail", "confirm", "refund", "receipt", "sbptoken", "kkt", "recurrent", "cancel"];
    return {
      webhookUrlPaths: webhookEvents.map((event) => ({
        event,
        path: `/webhooks/cloudkassir/${event}/${config.CLOUDKASSIR_WEBHOOK_SECRET}`
      }))
    };
  });

  app.get("/api/admin/export/:type", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const { type } = request.params as { type: string };
    const exportType = exportTypeSchema.parse(type);
    const rows = await buildExportRows(exportType);
    const filename = `${exportType}-${new Date().toISOString().slice(0, 10)}.csv`;

    return reply
      .header("Content-Type", "text/csv; charset=utf-8")
      .header("Content-Disposition", `attachment; filename="${filename}"`)
      .send(`\uFEFF${toCsv(rows)}`);
  });

  app.post("/api/admin/orders/:externalId/sync-ff", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const { externalId } = request.params as { externalId: string };
    const order = await prisma.order.findUnique({
      where: { externalId },
      select: { skladDispatchId: true }
    });
    if (!order) {
      return reply.code(404).send({ error: "Order not found" });
    }
    if (!order.skladDispatchId) {
      return reply.code(400).send({ error: "Order has no skladDispatchId" });
    }

    return ffSyncService.syncSkladDispatch(order.skladDispatchId, externalId);
  });

  app.post("/api/admin/orders/:externalId/receipts/second", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const { externalId } = request.params as { externalId: string };
    return receiptService.createSecondFullPayReceipt(externalId);
  });

  app.post("/api/admin/receipts/:id/refresh-status", async (request, reply) => {
    if (!ensureAdmin(request, reply)) {
      return;
    }

    const { id } = request.params as { id: string };
    const receiptId = Number(id);
    if (!Number.isInteger(receiptId) || receiptId <= 0) {
      return reply.code(400).send({ error: "Invalid receipt id" });
    }

    return receiptService.refreshCloudKassirStatus(receiptId);
  });
}

function ensureAdmin(request: FastifyRequest, reply: FastifyReply): boolean {
  if (isAdminAuthorized(request)) {
    return true;
  }

  reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
  return false;
}

async function countFiscalReceiptsByStatus(kind: string) {
  return prisma.fiscalReceipt.groupBy({
    by: ["status"],
    where: { kind },
    _count: { _all: true }
  });
}

function mapGroupCount(rows: Array<{ status: string; _count: { _all: number } }>): Record<string, number> {
  return Object.fromEntries(rows.map((row) => [row.status, row._count._all]));
}

function sumStatusCounts(rows: Array<{ status: string; _count: { _all: number } }>): number {
  return rows.reduce((sum, row) => sum + row._count._all, 0);
}

function latestByKind<T extends { kind: string }>(receipts: T[], kind: string): T | null {
  return receipts.find((receipt) => receipt.kind === kind) ?? null;
}

async function buildReturnReceiptRows(query: Pick<ListQuery, "search" | "status" | "take">) {
  const where: Prisma.CloudKassirReceiptWhereInput = {
    type: "IncomeReturn"
  };
  if (query.status) {
    where.status = query.status;
  }
  if (query.search) {
    where.OR = importedReceiptSearch(query.search);
  }

  const returnReceipts = await prisma.cloudKassirReceipt.findMany({
    where,
    include: {
      items: {
        select: {
          id: true,
          label: true,
          quantity: true,
          amountKop: true,
          rawMarkCode: true
        }
      }
    },
    orderBy: [{ dateTime: "desc" }, { importedAt: "desc" }],
    take: query.take
  });
  const invoiceIds = [
    ...new Set(
      returnReceipts
        .map((receipt) => receipt.invoiceId)
        .filter((invoiceId): invoiceId is string => Boolean(invoiceId))
    )
  ];
  const orders = await prisma.order.findMany({
    where: { externalId: { in: invoiceIds } },
    include: {
      items: {
        where: { isActive: true },
        select: {
          clientCode: true,
          name: true,
          quantity: true,
          amountKop: true,
          isMarked: true
        }
      },
      shipments: {
        orderBy: { updatedAt: "desc" },
        take: 1,
        select: {
          skladDispatchId: true,
          status: true,
          trackNumber: true,
          cdekNumber: true,
          cdekDeliveryStatus: true
        }
      },
      receipts: {
        orderBy: { createdAt: "asc" },
        select: {
          id: true,
          kind: true,
          status: true,
          requestId: true,
          cloudKassirId: true,
          errorMessage: true,
          createdAt: true,
          updatedAt: true
        }
      }
    }
  });
  const ordersByExternalId = new Map(orders.map((order) => [order.externalId, order]));
  const secondReceiptCloudKassirIds = [
    ...new Set(
      orders
        .flatMap((order) => order.receipts)
        .filter((receipt) => receipt.kind === "second_fullpay")
        .map((receipt) => receipt.cloudKassirId)
        .filter((cloudKassirId): cloudKassirId is string => Boolean(cloudKassirId))
    )
  ];
  const importedSecondReceipts = await prisma.cloudKassirReceipt.findMany({
    where: { cloudKassirId: { in: secondReceiptCloudKassirIds } },
    select: { cloudKassirId: true, dateTime: true, importedAt: true }
  });
  const importedSecondReceiptsById = new Map(importedSecondReceipts.map((receipt) => [receipt.cloudKassirId, receipt]));
  const secondFullPayStatuses = config.DRY_RUN ? ["dry_run", ...PROCESSED_RECEIPT_STATUSES] : PROCESSED_RECEIPT_STATUSES;
  const blockingSecondReturnStatuses = config.DRY_RUN ? ["dry_run", ...FINAL_RECEIPT_STATUSES] : FINAL_RECEIPT_STATUSES;

  return returnReceipts.map((returnReceipt) => {
    const order = returnReceipt.invoiceId ? ordersByExternalId.get(returnReceipt.invoiceId) ?? null : null;
    const secondReceipt =
      order?.receipts.find(
        (receipt) =>
          receipt.kind === "second_fullpay" &&
          secondFullPayStatuses.includes(receipt.status) &&
          (config.DRY_RUN || Boolean(receipt.cloudKassirId))
      ) ?? null;
    const importedSecondReceipt = secondReceipt?.cloudKassirId
      ? importedSecondReceiptsById.get(secondReceipt.cloudKassirId) ?? null
      : null;
    const returnAfterSecond = secondReceipt
      ? isReturnAfterSecondFullPay(returnReceipt, secondReceipt, importedSecondReceipt)
      : false;
    const returnRequestId = order ? `second-fullpay-return:${order.externalId}:${returnReceipt.cloudKassirId}:v1` : null;
    const secondReturnReceipt = returnRequestId
      ? order?.receipts.find((receipt) => receipt.requestId === returnRequestId) ?? null
      : null;
    const hasBlockingSecondReturn = Boolean(
      secondReturnReceipt && blockingSecondReturnStatuses.includes(secondReturnReceipt.status)
    );
    const needsSecondReturn = Boolean(
      order && secondReceipt && isProcessedImportedStatus(returnReceipt.status) && returnAfterSecond && !hasBlockingSecondReturn
    );

    return {
      cloudKassirId: returnReceipt.cloudKassirId,
      invoiceId: returnReceipt.invoiceId,
      accountId: returnReceipt.accountId,
      transactionId: returnReceipt.transactionId,
      type: returnReceipt.type,
      status: returnReceipt.status,
      amountKop: returnReceipt.amountKop,
      dateTime: returnReceipt.dateTime,
      documentNumber: returnReceipt.documentNumber,
      fiscalSign: returnReceipt.fiscalSign,
      url: returnReceipt.url,
      importedAt: returnReceipt.importedAt,
      itemsCount: returnReceipt.items.length,
      markedItemsCount: returnReceipt.items.filter((item) => item.rawMarkCode).length,
      order: order
        ? {
            id: order.id,
            externalId: order.externalId,
            amountKop: order.amountKop,
            paymentStatus: order.paymentStatus,
            skladDispatchId: order.skladDispatchId,
            deliveryTrackNumber: order.deliveryTrackNumber,
            customerEmail: order.customerEmail,
            customerPhone: order.customerPhone,
            items: order.items,
            latestShipment: order.shipments[0] ?? null
          }
        : null,
      secondReceipt,
      secondReturnReceipt,
      returnAfterSecondFullPay: returnAfterSecond,
      needsSecondReturn,
      reason: returnSecondFullPayReason({
        orderFound: Boolean(order),
        secondReceiptFound: Boolean(secondReceipt),
        returnAfterSecond,
        processedReturn: isProcessedImportedStatus(returnReceipt.status),
        hasBlockingSecondReturn
      }),
      detail: {
        returnReceipt,
        order,
        secondReceipt,
        secondReturnReceipt
      }
    };
  });
}

async function countPendingSecondFullPayReturns(): Promise<number> {
  const rows = await buildReturnReceiptRows({ take: 10000 });
  return rows.filter((row) => row.needsSecondReturn).length;
}

async function findFullyReturnedOrderExternalIds(): Promise<string[]> {
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
      (returnedByInvoiceId.get(receipt.invoiceId) ?? 0) + importedReceiptAmountKop(receipt)
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

function importedReceiptAmountKop(receipt: { amountKop: number | null; items: Array<{ amountKop: number | null }> }): number {
  if (receipt.amountKop !== null) {
    return receipt.amountKop;
  }

  return receipt.items.reduce((sum, item) => sum + (item.amountKop ?? 0), 0);
}

function returnSecondFullPayReason(input: {
  orderFound: boolean;
  secondReceiptFound: boolean;
  returnAfterSecond: boolean;
  processedReturn: boolean;
  hasBlockingSecondReturn: boolean;
}): string {
  if (!input.orderFound) {
    return "Заказ не найден";
  }
  if (!input.processedReturn) {
    return "Возвратный чек еще не обработан";
  }
  if (!input.secondReceiptFound) {
    return "Нет обработанного второго чека";
  }
  if (!input.returnAfterSecond) {
    return "Возврат был до второго чека";
  }
  if (input.hasBlockingSecondReturn) {
    return "Закрывающий возврат уже есть";
  }

  return "Нужен закрывающий возврат";
}

function isProcessedImportedStatus(status: string | null): boolean {
  return (status ?? "").toLowerCase() === "processed";
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

function orderSearch(search: string): Prisma.OrderWhereInput[] {
  const filters: Prisma.OrderWhereInput[] = [
    { externalId: { contains: search } },
    { accountId: { contains: search } },
    { customerEmail: { contains: search } },
    { customerPhone: { contains: search } },
    { deliveryTrackNumber: { contains: search } }
  ];
  const numericSearch = Number(search);
  if (Number.isInteger(numericSearch)) {
    filters.push({ skladDispatchId: numericSearch });
  }

  return filters;
}

function fiscalReceiptSearch(search: string): Prisma.FiscalReceiptWhereInput[] {
  return [
    { requestId: { contains: search } },
    { invoiceId: { contains: search } },
    { cloudKassirId: { contains: search } },
    { errorMessage: { contains: search } },
    { order: { externalId: { contains: search } } }
  ];
}

function importedReceiptSearch(search: string): Prisma.CloudKassirReceiptWhereInput[] {
  return [
    { cloudKassirId: { contains: search } },
    { invoiceId: { contains: search } },
    { accountId: { contains: search } },
    { transactionId: { contains: search } },
    { documentNumber: { contains: search } },
    { fiscalSign: { contains: search } },
    { rawJson: { contains: search } }
  ];
}

function markCodeSearch(search: string): Prisma.MarkCodeWhereInput[] {
  const filters: Prisma.MarkCodeWhereInput[] = [
    { code: { contains: search } },
    { orderItem: { clientCode: { contains: search } } },
    { orderItem: { name: { contains: search } } },
    { orderItem: { order: { externalId: { contains: search } } } },
    { shipmentProduct: { clientCode: { contains: search } } },
    { shipmentProduct: { name: { contains: search } } },
    { shipmentProduct: { shipment: { trackNumber: { contains: search } } } },
    { shipmentProduct: { shipment: { order: { externalId: { contains: search } } } } }
  ];
  const numericSearch = Number(search);
  if (Number.isInteger(numericSearch)) {
    filters.push({ shipmentProduct: { ffProductId: numericSearch } });
    filters.push({ shipmentProduct: { shipment: { skladDispatchId: numericSearch } } });
  }

  return filters;
}

function shipmentSearch(search: string): Prisma.ShipmentWhereInput[] {
  const filters: Prisma.ShipmentWhereInput[] = [
    { trackNumber: { contains: search } },
    { cdekNumber: { contains: search } },
    { cdekOrderNumber: { contains: search } },
    { cdekRecipientName: { contains: search } },
    { cdekRecipientPhone: { contains: search } },
    { cdekRecipientEmail: { contains: search } },
    { cdekStatus: { contains: search } },
    { cdekDeliveryStatus: { contains: search } },
    { order: { externalId: { contains: search } } },
    { products: { some: { clientCode: { contains: search } } } },
    { products: { some: { name: { contains: search } } } }
  ];
  const numericSearch = Number(search);
  if (Number.isInteger(numericSearch)) {
    filters.push({ skladDispatchId: numericSearch });
    filters.push({ shopId: numericSearch });
    filters.push({ skladId: numericSearch });
  }

  return filters;
}

async function buildCdekReport(query: ListQuery) {
  const where: Prisma.ShipmentWhereInput = {
    OR: [{ trackNumber: { not: null } }, { cdekNumber: { not: null } }, { rawCdekOrderJson: { not: null } }]
  };
  const and: Prisma.ShipmentWhereInput[] = [];

  if (query.status) {
    and.push({ OR: [{ cdekDeliveryStatus: query.status }, { cdekStatus: query.status }, { status: query.status }] });
  }
  if (query.search) {
    and.push({ OR: shipmentSearch(query.search) });
  }
  if (and.length > 0) {
    where.AND = and;
  }

  const shipments = await prisma.shipment.findMany({
    where,
    orderBy: { updatedAt: "desc" },
    take: 10000,
    include: {
      order: {
        include: {
          items: {
            where: { isActive: true },
            orderBy: { id: "asc" }
          }
        }
      },
      products: {
        where: { isActive: true },
        orderBy: { id: "asc" },
        include: {
          markCodes: {
            where: { isActive: true },
            select: {
              id: true,
              usedInSecondReceipt: true
            }
          }
        }
      }
    }
  });

  const duplicateIndexes = cdekDuplicateIndexes(shipments);
  const statusMap = new Map<string, { label: string; code: string; count: number; orderAmountKop: number; deliveryAmountKop: number }>();
  const skuMap = new Map<string, { sku: string; name: string; quantity: number; minPriceKop: number | null; maxPriceKop: number | null; amountKop: number }>();

  const rows = shipments.map((shipment) => {
    const raw = parseJsonObject(shipment.rawCdekOrderJson);
    const entity = cdekEntity(raw);
    const latestStatus = latestRawCdekStatus(entity);
    const statusCode = shipment.cdekDeliveryStatus ?? latestStatus.code ?? shipment.cdekStatus ?? shipment.status ?? "unknown";
    const statusLabel = cdekStatusLabel(statusCode, latestStatus.name);
    const deliveryAmountKop = cdekDeliveryAmountKop(entity);
    const orderAmountKop = shipment.order?.amountKop ?? 0;
    const productAmountKop = shipment.order?.items.reduce((sum, item) => sum + item.amountKop, 0) ?? 0;
    const totalAmountKop = productAmountKop + deliveryAmountKop;
    const duplicate = duplicateIndexes.get(shipment.id);
    const itemLines = cdekOrderItemLines(shipment.order?.items ?? [], entity, shipment.products);
    const marksCount = shipment.products.reduce((sum, product) => sum + product.markCodes.length, 0);
    const usedMarksCount = shipment.products.reduce(
      (sum, product) => sum + product.markCodes.filter((markCode) => markCode.usedInSecondReceipt).length,
      0
    );

    const statusAgg = statusMap.get(statusCode) ?? {
      label: statusLabel,
      code: statusCode,
      count: 0,
      orderAmountKop: 0,
      deliveryAmountKop: 0
    };
    statusAgg.count += 1;
    statusAgg.orderAmountKop += orderAmountKop;
    statusAgg.deliveryAmountKop += deliveryAmountKop;
    statusMap.set(statusCode, statusAgg);

    for (const item of shipment.order?.items ?? []) {
      const skuKey = `${item.clientCode}\u0000${item.name}`;
      const current = skuMap.get(skuKey) ?? {
        sku: item.clientCode,
        name: item.name,
        quantity: 0,
        minPriceKop: null,
        maxPriceKop: null,
        amountKop: 0
      };
      current.quantity += item.quantity;
      current.amountKop += item.amountKop;
      current.minPriceKop = current.minPriceKop === null ? item.unitPriceKop : Math.min(current.minPriceKop, item.unitPriceKop);
      current.maxPriceKop = current.maxPriceKop === null ? item.unitPriceKop : Math.max(current.maxPriceKop, item.unitPriceKop);
      skuMap.set(skuKey, current);
    }

    return {
      id: shipment.id,
      skladDispatchId: shipment.skladDispatchId,
      date: cdekDate(shipment.dateCreated, shipment.createdAt),
      orderExternalId: shipment.order?.externalId ?? shipment.cdekOrderNumber,
      type: duplicate?.isRepeat ? "Повторный" : "Первичный",
      duplicateGroupSize: duplicate?.groupSize ?? 1,
      duplicateKey: duplicate?.key ?? null,
      recipientName: shipment.cdekRecipientName,
      recipientPhone: shipment.cdekRecipientPhone,
      recipientEmail: shipment.cdekRecipientEmail,
      trackNumber: shipment.trackNumber ?? shipment.cdekNumber,
      cdekNumber: shipment.cdekNumber,
      cdekOrderNumber: shipment.cdekOrderNumber,
      ffCdekStatus: shipment.cdekStatus,
      cdekDeliveryStatus: shipment.cdekDeliveryStatus,
      statusCode,
      statusLabel,
      city: cdekCity(entity, latestStatus.city),
      plannedDeliveryDate: cdekPlannedDeliveryDate(entity),
      deliveredDate: cdekDeliveredDate(entity),
      composition: itemLines.join("\n"),
      items: itemLines,
      productsCount: shipment.order?.items.length ?? shipment.products.length,
      marksCount,
      usedMarksCount,
      orderAmountKop,
      productAmountKop,
      deliveryAmountKop,
      totalAmountKop,
      updatedAt: shipment.updatedAt,
      detail: {
        shipment,
        cdekEntity: entity
      }
    };
  });

  const delivered = rows.filter((row) => row.statusCode === "DELIVERED" || row.statusCode.toLowerCase() === "delivered").length;
  const synced = rows.filter((row) => row.recipientPhone || row.recipientName || row.cdekNumber).length;
  const repeatRows = rows.filter((row) => row.type === "Повторный");
  const duplicateGroups = new Set(
    rows
      .filter((row) => row.duplicateGroupSize > 1 && row.duplicateKey)
      .map((row) => row.duplicateKey)
  ).size;
  const statusRows = [...statusMap.values()].sort((left, right) => right.count - left.count);
  const skuRows = [...skuMap.values()]
    .sort((left, right) => right.amountKop - left.amountKop)
    .slice(0, 80);

  return {
    generatedAt: new Date().toISOString(),
    summary: {
      tracks: rows.length,
      synced,
      delivered,
      deliveredPercent: rows.length > 0 ? Number(((delivered / rows.length) * 100).toFixed(1)) : 0,
      inTransit: rows.length - delivered,
      waitingCheck: rows.length - synced,
      repeatOrders: repeatRows.length,
      duplicateGroups,
      orderAmountKop: rows.reduce((sum, row) => sum + row.orderAmountKop, 0),
      productAmountKop: rows.reduce((sum, row) => sum + row.productAmountKop, 0),
      deliveryAmountKop: rows.reduce((sum, row) => sum + row.deliveryAmountKop, 0),
      repeatAmountKop: repeatRows.reduce((sum, row) => sum + row.totalAmountKop, 0),
      repeatDeliveryAmountKop: repeatRows.reduce((sum, row) => sum + row.deliveryAmountKop, 0)
    },
    statusRows,
    skuRows,
    rows: rows.slice(0, query.take)
  };
}

async function buildExportRows(type: ExportType): Promise<ExportRow[]> {
  if (type === "returns") {
    const rows = await buildReturnReceiptRows({ take: 10000 });
    return rows.map((row) => ({
      cloudKassirId: row.cloudKassirId,
      invoiceId: row.invoiceId,
      amountRub: row.amountKop === null ? null : row.amountKop / 100,
      status: row.status,
      dateTime: row.dateTime,
      documentNumber: row.documentNumber,
      fiscalSign: row.fiscalSign,
      orderExternalId: row.order?.externalId,
      orderAmountRub: row.order ? row.order.amountKop / 100 : null,
      skladDispatchId: row.order?.skladDispatchId,
      trackNumber: row.order?.deliveryTrackNumber ?? row.order?.latestShipment?.trackNumber ?? row.order?.latestShipment?.cdekNumber,
      secondReceiptStatus: row.secondReceipt?.status,
      secondReceiptCloudKassirId: row.secondReceipt?.cloudKassirId,
      secondReturnReceiptStatus: row.secondReturnReceipt?.status,
      secondReturnReceiptCloudKassirId: row.secondReturnReceipt?.cloudKassirId,
      needsSecondReturn: row.needsSecondReturn,
      reason: row.reason,
      returnAfterSecondFullPay: row.returnAfterSecondFullPay,
      itemsCount: row.itemsCount,
      markedItemsCount: row.markedItemsCount,
      importedAt: row.importedAt
    }));
  }

  if (type === "cdek") {
    const report = await buildCdekReport({ take: 10000 });
    return report.rows.map((row) => ({
      date: row.date,
      orderExternalId: row.orderExternalId,
      type: row.type,
      recipientName: row.recipientName,
      recipientPhone: row.recipientPhone,
      recipientEmail: row.recipientEmail,
      trackNumber: row.trackNumber,
      status: row.statusLabel,
      statusCode: row.statusCode,
      ffCdekStatus: row.ffCdekStatus,
      cdekDeliveryStatus: row.cdekDeliveryStatus,
      city: row.city,
      plannedDeliveryDate: row.plannedDeliveryDate,
      deliveredDate: row.deliveredDate,
      composition: row.composition,
      productsRub: kopToRub(row.productAmountKop),
      deliveryRub: kopToRub(row.deliveryAmountKop),
      totalRub: kopToRub(row.totalAmountKop),
      skladDispatchId: row.skladDispatchId,
      cdekOrderNumber: row.cdekOrderNumber,
      updatedAt: row.updatedAt
    }));
  }

  if (type === "orders") {
    const rows = await prisma.order.findMany({
      orderBy: { updatedAt: "desc" },
      take: 5000,
      include: {
        items: { where: { isActive: true } },
        shipments: { orderBy: { updatedAt: "desc" }, take: 1 },
        receipts: { orderBy: { updatedAt: "desc" } }
      }
    });

    return rows.map((order) => {
      const firstReceipt = latestByKind(order.receipts, "first_prepayment");
      const secondReceipt = latestByKind(order.receipts, "second_fullpay");
      const shipment = order.shipments[0];
      const markedRequired = order.items.reduce((sum, item) => sum + (item.isMarked ? item.quantity : 0), 0);

      return {
        externalId: order.externalId,
        accountId: order.accountId,
        paymentStatus: order.paymentStatus,
        amountRub: order.amountKop / 100,
        currency: order.currency,
        customerEmail: order.customerEmail,
        customerPhone: order.customerPhone,
        deliveryTrackNumber: order.deliveryTrackNumber,
        skladDispatchId: order.skladDispatchId,
        markedRequired,
        itemsCount: order.items.length,
        firstReceiptStatus: firstReceipt?.status,
        firstReceiptCloudKassirId: firstReceipt?.cloudKassirId,
        secondReceiptStatus: secondReceipt?.status,
        secondReceiptCloudKassirId: secondReceipt?.cloudKassirId,
        shipmentStatus: shipment?.status,
        cdekStatus: shipment?.cdekStatus,
        cdekDeliveryStatus: shipment?.cdekDeliveryStatus,
        cdekRecipientName: shipment?.cdekRecipientName,
        cdekRecipientPhone: shipment?.cdekRecipientPhone,
        cdekRecipientEmail: shipment?.cdekRecipientEmail,
        createdAt: order.createdAt,
        updatedAt: order.updatedAt
      };
    });
  }

  if (type === "fiscal-receipts") {
    const rows = await prisma.fiscalReceipt.findMany({
      orderBy: { updatedAt: "desc" },
      take: 5000,
      include: { order: { select: { externalId: true } } }
    });

    return rows.map((receipt) => ({
      id: receipt.id,
      orderExternalId: receipt.order?.externalId,
      kind: receipt.kind,
      status: receipt.status,
      requestId: receipt.requestId,
      invoiceId: receipt.invoiceId,
      cloudKassirId: receipt.cloudKassirId,
      errorMessage: receipt.errorMessage,
      createdAt: receipt.createdAt,
      updatedAt: receipt.updatedAt
    }));
  }

  if (type === "imported-receipts") {
    const rows = await prisma.cloudKassirReceipt.findMany({
      orderBy: { importedAt: "desc" },
      take: 5000,
      include: { items: true }
    });

    return rows.map((receipt) => ({
      cloudKassirId: receipt.cloudKassirId,
      invoiceId: receipt.invoiceId,
      accountId: receipt.accountId,
      transactionId: receipt.transactionId,
      type: receipt.type,
      status: receipt.status,
      amountRub: receipt.amountKop === null ? null : receipt.amountKop / 100,
      dateTime: receipt.dateTime,
      documentNumber: receipt.documentNumber,
      fiscalSign: receipt.fiscalSign,
      fiscalNumber: receipt.fiscalNumber,
      regNumber: receipt.regNumber,
      inn: receipt.inn,
      ofd: receipt.ofd,
      url: receipt.url,
      qrCodeUrl: receipt.qrCodeUrl,
      itemsCount: receipt.items.length,
      markedItemsCount: receipt.items.filter((item) => item.rawMarkCode).length,
      importedAt: receipt.importedAt,
      updatedAt: receipt.updatedAt
    }));
  }

  if (type === "transactions") {
    const rows = await prisma.cloudPaymentTransaction.findMany({
      orderBy: { updatedAt: "desc" },
      take: 5000
    });

    return rows.map((transaction) => ({
      transactionId: transaction.transactionId,
      invoiceId: transaction.invoiceId,
      accountId: transaction.accountId,
      email: transaction.email,
      amountRub: transaction.amountKop === null ? null : transaction.amountKop / 100,
      currency: transaction.currency,
      operationType: transaction.operationType,
      status: transaction.status,
      statusCode: transaction.statusCode,
      reason: transaction.reason,
      reasonCode: transaction.reasonCode,
      cardFirstSix: transaction.cardFirstSix,
      cardLastFour: transaction.cardLastFour,
      cardType: transaction.cardType,
      paymentDate: transaction.paymentDate,
      importedAt: transaction.importedAt,
      updatedAt: transaction.updatedAt
    }));
  }

  if (type === "webhook-events") {
    const rows = await prisma.apiEvent.findMany({
      orderBy: { createdAt: "desc" },
      take: 5000
    });

    return rows.map((event) => ({
      id: event.id,
      source: event.source,
      eventType: event.eventType,
      externalId: event.externalId,
      payloadJson: event.payloadJson,
      createdAt: event.createdAt
    }));
  }

  if (type === "mark-codes") {
    const rows = await prisma.markCode.findMany({
      orderBy: { updatedAt: "desc" },
      take: 10000,
      include: {
        orderItem: {
          select: {
            clientCode: true,
            name: true,
            isMarked: true,
            isActive: true,
            order: { select: { externalId: true } }
          }
        },
        shipmentProduct: {
          select: {
            ffProductId: true,
            clientCode: true,
            name: true,
            shipment: {
              select: {
                skladDispatchId: true,
                status: true,
                trackNumber: true,
                cdekNumber: true,
                cdekRecipientName: true,
                cdekDeliveryStatus: true,
                order: { select: { externalId: true } }
              }
            }
          }
        }
      }
    });

    return rows.map((markCode) => ({
      id: markCode.id,
      code: markCode.code,
      isActive: markCode.isActive,
      usedInSecondReceipt: markCode.usedInSecondReceipt,
      validationStatus: markCode.validationStatus,
      orderExternalId: markCode.orderItem?.order?.externalId ?? markCode.shipmentProduct.shipment.order?.externalId,
      orderItemClientCode: markCode.orderItem?.clientCode,
      orderItemName: markCode.orderItem?.name,
      orderItemIsMarked: markCode.orderItem?.isMarked,
      orderItemIsActive: markCode.orderItem?.isActive,
      ffProductId: markCode.shipmentProduct.ffProductId,
      ffClientCode: markCode.shipmentProduct.clientCode,
      ffProductName: markCode.shipmentProduct.name,
      skladDispatchId: markCode.shipmentProduct.shipment.skladDispatchId,
      shipmentStatus: markCode.shipmentProduct.shipment.status,
      trackNumber: markCode.shipmentProduct.shipment.trackNumber ?? markCode.shipmentProduct.shipment.cdekNumber,
      cdekRecipientName: markCode.shipmentProduct.shipment.cdekRecipientName,
      createdAt: markCode.createdAt,
      updatedAt: markCode.updatedAt
    }));
  }

  if (type === "shipments" || type === "tracks") {
    const rows = await prisma.shipment.findMany({
      where: type === "tracks" ? { OR: [{ trackNumber: { not: null } }, { cdekNumber: { not: null } }] } : undefined,
      orderBy: { updatedAt: "desc" },
      take: 10000,
      include: {
        order: { select: { externalId: true } },
        products: {
          where: { isActive: true },
          include: {
            markCodes: {
              where: { isActive: true },
              select: { id: true, usedInSecondReceipt: true }
            }
          }
        }
      }
    });

    return rows.map((shipment) => {
      const markCodesCount = shipment.products.reduce((sum, product) => sum + product.markCodes.length, 0);
      const usedMarkCodesCount = shipment.products.reduce(
        (sum, product) => sum + product.markCodes.filter((markCode) => markCode.usedInSecondReceipt).length,
        0
      );

      return {
        skladDispatchId: shipment.skladDispatchId,
        orderExternalId: shipment.order?.externalId,
        status: shipment.status,
        cdekStatus: shipment.cdekStatus,
        cdekDeliveryStatus: shipment.cdekDeliveryStatus,
        trackNumber: shipment.trackNumber,
        cdekNumber: shipment.cdekNumber,
        cdekOrderNumber: shipment.cdekOrderNumber,
        cdekRecipientName: shipment.cdekRecipientName,
        cdekRecipientPhone: shipment.cdekRecipientPhone,
        cdekRecipientEmail: shipment.cdekRecipientEmail,
        shopId: shipment.shopId,
        skladId: shipment.skladId,
        productsCount: shipment.products.length,
        markCodesCount,
        usedMarkCodesCount,
        dateCreated: shipment.dateCreated,
        dateAtWork: shipment.dateAtWork,
        dateAtShipped: shipment.dateAtShipped,
        dateClosed: shipment.dateClosed,
        createdAt: shipment.createdAt,
        updatedAt: shipment.updatedAt
      };
    });
  }

  return [];
}

type CdekDuplicateShipment = {
  id: number;
  createdAt: Date;
  updatedAt: Date;
  cdekRecipientPhone: string | null;
  cdekRecipientEmail: string | null;
  order: {
    customerPhone: string | null;
    customerEmail: string | null;
  } | null;
};

type JsonRecord = Record<string, unknown>;

const CDEK_STATUS_LABELS: Record<string, string> = {
  ACCEPTED: "Принят",
  ACCEPTED_AT_PICK_UP_POINT: "Принят на склад до востребования",
  CREATED: "Создан",
  DELIVERED: "Вручен",
  NOT_DELIVERED: "Не вручен",
  READY_FOR_SHIPMENT_IN_SENDER_CITY: "Выдан на отправку в г. отправителе",
  READY_FOR_SHIPMENT_IN_TRANSIT_CITY: "Выдан на отправку в г. транзите",
  SENT_TO_RECIPIENT_CITY: "Отправлен в г. получатель",
  SENT_TO_TRANSIT_CITY: "Отправлен в г. транзит"
};

function cdekDuplicateIndexes(shipments: CdekDuplicateShipment[]): Map<number, { isRepeat: boolean; groupSize: number; key: string }> {
  const groups = new Map<string, CdekDuplicateShipment[]>();

  for (const shipment of shipments) {
    const key = cdekDuplicateKey(shipment);
    if (!key) {
      continue;
    }

    groups.set(key, [...(groups.get(key) ?? []), shipment]);
  }

  const result = new Map<number, { isRepeat: boolean; groupSize: number; key: string }>();
  for (const [key, group] of groups) {
    if (group.length < 2) {
      continue;
    }

    const orderedGroup = [...group].sort((left, right) => left.createdAt.getTime() - right.createdAt.getTime());
    orderedGroup.forEach((shipment, index) => {
      result.set(shipment.id, { isRepeat: index > 0, groupSize: orderedGroup.length, key });
    });
  }

  return result;
}

function cdekDuplicateKey(shipment: CdekDuplicateShipment): string | null {
  const phone = normalizeDigits(shipment.cdekRecipientPhone ?? shipment.order?.customerPhone);
  if (phone.length >= 7) {
    return `phone:${phone.slice(-10)}`;
  }

  const email = (shipment.cdekRecipientEmail ?? shipment.order?.customerEmail ?? "").trim().toLowerCase();
  return email ? `email:${email}` : null;
}

function parseJsonObject(value: string | null): JsonRecord {
  if (!value) {
    return {};
  }

  try {
    const parsed = JSON.parse(value) as unknown;
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function cdekEntity(raw: JsonRecord): JsonRecord {
  return isRecord(raw.entity) ? raw.entity : raw;
}

function latestRawCdekStatus(entity: JsonRecord): { code?: string; name?: string; dateTime?: string; city?: string } {
  const statuses = Array.isArray(entity.statuses) ? entity.statuses.filter(isRecord) : [];
  const latest = statuses[statuses.length - 1];
  if (!latest) {
    return {};
  }

  return {
    code: asString(latest.code),
    name: asString(latest.name),
    dateTime: asString(latest.date_time),
    city: asString(latest.city)
  };
}

function cdekStatusLabel(code: string, fallback?: string): string {
  return CDEK_STATUS_LABELS[code] ?? fallback ?? code;
}

function cdekCity(entity: JsonRecord, fallback?: string): string | undefined {
  return (
    fallback ??
    firstString(entity, [
      ["to_location", "city"],
      ["recipient_location", "city"],
      ["delivery_detail", "recipient_city"],
      ["delivery_detail", "city"],
      ["location", "city"]
    ])
  );
}

function cdekPlannedDeliveryDate(entity: JsonRecord): string | undefined {
  return firstString(entity, [
    ["planned_delivery_date"],
    ["delivery_detail", "planned_delivery_date"],
    ["delivery_detail", "date_max"],
    ["delivery_detail", "date_min"]
  ]);
}

function cdekDeliveredDate(entity: JsonRecord): string | undefined {
  const statuses = Array.isArray(entity.statuses) ? entity.statuses.filter(isRecord) : [];
  const delivered = [...statuses].reverse().find((status) => asString(status.code) === "DELIVERED");
  return asString(delivered?.date_time) ?? firstString(entity, [["delivery_date"], ["delivery_detail", "delivery_date"]]);
}

function cdekDeliveryAmountKop(entity: JsonRecord): number {
  const amountRub = firstNumber(entity, [
    ["delivery_detail", "delivery_sum"],
    ["delivery_detail", "total_sum"],
    ["delivery_sum"],
    ["total_sum"],
    ["delivery_recipient_cost", "value"],
    ["recipient_cost", "value"]
  ]);

  return amountRub === undefined ? 0 : rubToKop(amountRub);
}

function cdekDate(dateCreated: string | null, createdAt: Date): string {
  return dateCreated || createdAt.toISOString();
}

function cdekOrderItemLines(
  orderItems: Array<{ name: string; quantity: number; amountKop: number }>,
  entity: JsonRecord,
  shipmentProducts: Array<{ name: string; clientCode: string; factQty: number }>
): string[] {
  if (orderItems.length > 0) {
    return orderItems.map((item) => `${item.name} x ${item.quantity} = ${kopToRub(item.amountKop).toFixed(2)} ₽`);
  }

  const packageLines = cdekPackageItemLines(entity);
  if (packageLines.length > 0) {
    return packageLines;
  }

  return shipmentProducts.map((product) => `${product.name || product.clientCode} x ${product.factQty}`);
}

function cdekPackageItemLines(entity: JsonRecord): string[] {
  const packages = Array.isArray(entity.packages) ? entity.packages.filter(isRecord) : [];
  return packages.flatMap((pkg) => {
    const items = Array.isArray(pkg.items) ? pkg.items.filter(isRecord) : [];
    return items.map((item) => {
      const name = asString(item.name) ?? asString(item.ware_key) ?? "Товар";
      const quantity = firstNumber(item, [["amount"], ["quantity"]]) ?? 1;
      const cost = firstNumber(item, [["cost"], ["payment", "value"]]);
      return cost === undefined ? `${name} x ${quantity}` : `${name} x ${quantity} = ${cost.toFixed(2)} ₽`;
    });
  });
}

function firstString(root: JsonRecord, paths: string[][]): string | undefined {
  for (const path of paths) {
    const value = valueAtPath(root, path);
    const text = asString(value);
    if (text) {
      return text;
    }
  }

  return undefined;
}

function firstNumber(root: JsonRecord, paths: string[][]): number | undefined {
  for (const path of paths) {
    const value = valueAtPath(root, path);
    const number = asNumber(value);
    if (number !== undefined) {
      return number;
    }
  }

  return undefined;
}

function valueAtPath(root: JsonRecord, path: string[]): unknown {
  let current: unknown = root;
  for (const key of path) {
    if (!isRecord(current)) {
      return undefined;
    }

    current = current[key];
  }

  return current;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const normalized = Number(value.replace(",", "."));
    return Number.isFinite(normalized) ? normalized : undefined;
  }

  return undefined;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeDigits(value: string | null | undefined): string {
  return value?.replace(/\D/g, "") ?? "";
}

function toCsv(rows: ExportRow[]): string {
  if (rows.length === 0) {
    return "empty\n";
  }

  const headers = Object.keys(rows[0]);
  return [
    headers.map(csvCell).join(";"),
    ...rows.map((row) => headers.map((header) => csvCell(row[header])).join(";"))
  ].join("\n");
}

function csvCell(value: unknown): string {
  if (value === null || value === undefined) {
    return "";
  }

  const text = value instanceof Date ? value.toISOString() : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}
