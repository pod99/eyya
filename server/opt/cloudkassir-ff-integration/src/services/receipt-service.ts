import { Prisma } from "@prisma/client";
import { CloudKassirClient } from "../clients/cloudkassir.js";
import { assertReadyForLiveCloudKassir, config } from "../config.js";
import { prisma } from "../db.js";
import {
  buildFirstPrepaymentReceipt,
  buildSecondFullPayReceipt,
  buildSecondFullPayReturnReceipt
} from "../fiscal/build-receipts.js";
import { stringifyJson } from "../utils/json.js";

const FINAL_RECEIPT_STATUSES = ["sent", "queued", "processed"];
const PROCESSED_RECEIPT_STATUSES = ["processed"];
type FiscalOrder = Parameters<typeof buildSecondFullPayReceipt>[0];
type FiscalOrderItem = FiscalOrder["items"][number];
type ProcessedReturnReceipt = {
  cloudKassirId: string;
  amountKop: number | null;
  dateTime: string | null;
  importedAt: Date;
  items: Array<{
    label: string | null;
    priceKop: number | null;
    quantity: string | null;
    amountKop: number | null;
  }>;
};

export class ReceiptService {
  constructor(private readonly cloudKassirClient = new CloudKassirClient()) {}

  async createFirstPrepaymentReceipt(orderExternalId: string) {
    const order = await prisma.order.findUniqueOrThrow({
      where: { externalId: orderExternalId },
      include: { items: { where: { isActive: true } } }
    });

    const existing = await prisma.fiscalReceipt.findFirst({
      where: {
        orderId: order.id,
        kind: "first_prepayment",
        status: { in: FINAL_RECEIPT_STATUSES }
      }
    });

    if (existing) {
      return existing;
    }

    validateOrderHasActiveItems(order);

    const payload = buildFirstPrepaymentReceipt(order);
    const requestId = `first-prepayment:${order.externalId}:v1`;

    return this.sendOrDryRun({
      orderId: order.id,
      kind: "first_prepayment",
      requestId,
      invoiceId: order.externalId,
      payload
    });
  }

  async createSecondFullPayReceipt(orderExternalId: string) {
    const order = await prisma.order.findUniqueOrThrow({
      where: { externalId: orderExternalId },
      include: { items: { where: { isActive: true } } }
    });

    const existing = await prisma.fiscalReceipt.findFirst({
      where: {
        orderId: order.id,
        kind: "second_fullpay",
        status: { in: FINAL_RECEIPT_STATUSES }
      }
    });

    if (existing) {
      return existing;
    }

    const preSecondReturnReceipts = await loadProcessedIncomeReturnReceipts(order.externalId);
    const fiscalOrder = adjustOrderForPreSecondReturns(order, preSecondReturnReceipts);
    if (!fiscalOrder) {
      throw new Error("Order was fully returned before second full-pay receipt; second full-pay receipt is skipped");
    }

    validateOrderHasActiveItems(fiscalOrder);
    await validateShipmentReadyForSecondReceipt(order.id);
    await validateNoUnsupportedActiveMarkCodes(order.id);

    const markCodes = await prisma.markCode.findMany({
      where: {
        isActive: true,
        shipmentProduct: {
          isActive: true,
          shipment: {
            orderId: order.id
          }
        },
        orderItem: {
          isActive: true,
          isMarked: true,
          orderId: order.id
        }
      },
      include: { orderItem: true }
    });

    validateMarkedItemsReady(fiscalOrder, markCodes);

    const payload = buildSecondFullPayReceipt(fiscalOrder, markCodes);
    const requestId = `second-fullpay:${order.externalId}:v1`;

    const receipt = await this.sendOrDryRun({
      orderId: order.id,
      kind: "second_fullpay",
      requestId,
      invoiceId: order.externalId,
      payload,
      markCodeIdsToMarkUsed: markCodes.map((markCode) => markCode.id)
    });

    return receipt;
  }

  async createSecondFullPayReturnReceipt(orderExternalId: string, returnCloudKassirReceiptId: string) {
    const order = await prisma.order.findUniqueOrThrow({
      where: { externalId: orderExternalId },
      include: { items: { where: { isActive: true } } }
    });

    const returnReceipt = await prisma.cloudKassirReceipt.findUniqueOrThrow({
      where: { cloudKassirId: returnCloudKassirReceiptId },
      include: { items: true }
    });

    if (returnReceipt.invoiceId !== order.externalId) {
      throw new Error(
        `Return receipt ${returnReceipt.cloudKassirId} invoiceId=${returnReceipt.invoiceId} does not match order ${order.externalId}`
      );
    }
    if (returnReceipt.type !== "IncomeReturn") {
      throw new Error(`Receipt ${returnReceipt.cloudKassirId} is not IncomeReturn`);
    }
    if (!isProcessedImportedReceiptStatus(returnReceipt.status)) {
      throw new Error(`Return receipt ${returnReceipt.cloudKassirId} is not processed yet`);
    }

    const requestId = `second-fullpay-return:${order.externalId}:${returnReceipt.cloudKassirId}:v1`;
    const existing = await prisma.fiscalReceipt.findUnique({
      where: { requestId }
    });
    if (existing && FINAL_RECEIPT_STATUSES.includes(existing.status)) {
      return existing;
    }

    const existingForImportedReturn = await prisma.fiscalReceipt.findFirst({
      where: {
        orderId: order.id,
        kind: "second_fullpay_return",
        cloudKassirId: returnReceipt.cloudKassirId,
        status: { in: FINAL_RECEIPT_STATUSES }
      },
      orderBy: { createdAt: "asc" }
    });
    if (existingForImportedReturn) {
      return existingForImportedReturn;
    }

    validateOrderHasActiveItems(order);

    const secondFullPayReceipt = await prisma.fiscalReceipt.findFirst({
      where: {
        orderId: order.id,
        kind: "second_fullpay",
        status: { in: config.DRY_RUN ? ["dry_run", ...PROCESSED_RECEIPT_STATUSES] : PROCESSED_RECEIPT_STATUSES }
      },
      orderBy: { createdAt: "asc" }
    });

    if (!secondFullPayReceipt) {
      throw new Error("Cannot create full-pay return receipt before second full-pay receipt");
    }

    const importedSecondFullPayReceipt = secondFullPayReceipt.cloudKassirId
      ? await prisma.cloudKassirReceipt.findUnique({
          where: { cloudKassirId: secondFullPayReceipt.cloudKassirId },
          select: { dateTime: true, importedAt: true }
        })
      : null;
    if (!isReturnAfterSecondFullPay(returnReceipt, secondFullPayReceipt, importedSecondFullPayReceipt)) {
      throw new Error(
        `Return receipt ${returnReceipt.cloudKassirId} happened before the second full-pay receipt; no full-pay return is required`
      );
    }

    const previousReturnPayloads = await prisma.fiscalReceipt.findMany({
      where: {
        orderId: order.id,
        kind: "second_fullpay_return",
        status: { in: FINAL_RECEIPT_STATUSES },
        requestId: { not: requestId }
      },
      select: { payloadJson: true },
      orderBy: { createdAt: "asc" }
    });
    const payload = buildSecondFullPayReturnReceipt(
      order,
      secondFullPayReceipt.payloadJson,
      returnReceipt,
      previousReturnPayloads.map((receipt) => receipt.payloadJson)
    );

    return this.sendOrDryRun({
      orderId: order.id,
      kind: "second_fullpay_return",
      requestId,
      invoiceId: order.externalId,
      payload
    });
  }

  async refreshCloudKassirStatus(receiptId: number) {
    const receipt = await prisma.fiscalReceipt.findUniqueOrThrow({
      where: { id: receiptId }
    });

    if (!receipt.cloudKassirId) {
      return receipt;
    }

    const response = await this.cloudKassirClient.getReceiptStatus(receipt.cloudKassirId);
    if (!response.Success) {
      return prisma.fiscalReceipt.update({
        where: { id: receipt.id },
        data: {
          responseJson: stringifyJson(response),
          errorMessage: response.Message ?? "CloudKassir status request was unsuccessful"
        }
      });
    }

    const status = normalizeCloudKassirStatus(response.Model);

    return prisma.fiscalReceipt.update({
      where: { id: receipt.id },
      data: {
        status,
        responseJson: stringifyJson(response),
        errorMessage: response.Message ?? null
      }
    });
  }

  private async sendOrDryRun(input: {
    orderId: number;
    kind: string;
    requestId: string;
    invoiceId: string;
    payload: unknown;
    markCodeIdsToMarkUsed?: number[];
  }) {
    const payloadJson = stringifyJson(input.payload);
    const markCodeIdsToMarkUsed = input.markCodeIdsToMarkUsed ?? [];

    if (config.DRY_RUN) {
      return prisma.$transaction(async (tx) => {
        const receipt = await tx.fiscalReceipt.upsert({
          where: { requestId: input.requestId },
          create: {
            orderId: input.orderId,
            kind: input.kind,
            requestId: input.requestId,
            invoiceId: input.invoiceId,
            status: "dry_run",
            payloadJson
          },
          update: {
            status: "dry_run",
            payloadJson
          }
        });

        if (shouldMarkCodesUsed(receipt) && markCodeIdsToMarkUsed.length > 0) {
          await tx.markCode.updateMany({
            where: { id: { in: markCodeIdsToMarkUsed } },
            data: { usedInSecondReceipt: true }
          });
        }

        return receipt;
      });
    }

    assertReadyForLiveCloudKassir();
    let existing = await prisma.fiscalReceipt.findUnique({
      where: { requestId: input.requestId }
    });
    if (existing && ["sent", "queued", "processed"].includes(existing.status)) {
      return existing;
    }

    if (existing?.status === "sending") {
      if (!isSendingTimedOut(existing.updatedAt)) {
        return existing;
      }

      existing = await prisma.fiscalReceipt.update({
        where: { requestId: input.requestId },
        data: {
          status: "error",
          errorMessage: "Sending timed out before CloudKassir response; retrying"
        }
      });
    }

    if (!existing) {
      try {
        await prisma.fiscalReceipt.create({
          data: {
            orderId: input.orderId,
            kind: input.kind,
            requestId: input.requestId,
            invoiceId: input.invoiceId,
            status: "sending",
            payloadJson
          }
        });
      } catch (error) {
        if (error instanceof Prisma.PrismaClientKnownRequestError && error.code === "P2002") {
          return prisma.fiscalReceipt.findUniqueOrThrow({
            where: { requestId: input.requestId }
          });
        }

        throw error;
      }
    } else {
      await prisma.fiscalReceipt.update({
        where: { requestId: input.requestId },
        data: {
          status: "sending",
          payloadJson,
          errorMessage: null
        }
      });
    }

    const response = await this.cloudKassirClient.createReceipt(input.payload as never, input.requestId);
    const cloudKassirId = extractReceiptId(response.Model);
    const acceptedWithoutReceiptId = response.Success && !cloudKassirId;

    return prisma.$transaction(async (tx) => {
      const receipt = await tx.fiscalReceipt.update({
        where: { requestId: input.requestId },
        data: {
          cloudKassirId,
          status: cloudKassirId || acceptedWithoutReceiptId ? "sent" : "error",
          payloadJson,
          responseJson: stringifyJson(response),
          errorMessage: response.Success
            ? acceptedWithoutReceiptId
              ? "CloudKassir returned Success=true without receipt Id; not retrying to avoid duplicate fiscal receipt"
              : null
            : response.Message ?? "CloudKassir returned unsuccessful response"
        }
      });

      if (shouldMarkCodesUsed(receipt) && markCodeIdsToMarkUsed.length > 0) {
        await tx.markCode.updateMany({
          where: { id: { in: markCodeIdsToMarkUsed } },
          data: { usedInSecondReceipt: true }
        });
      }

      return receipt;
    });
  }
}

async function validateShipmentReadyForSecondReceipt(orderId: number): Promise<void> {
  const readyShipment = await prisma.shipment.findFirst({
    where: {
      orderId,
      status: { in: ["otgrugen", "close"] },
      OR: [{ cdekStatus: null }, { cdekStatus: { in: ["sended", "close", "closed"] } }]
    }
  });

  if (!readyShipment) {
    throw new Error("No completed FF Platform shipment found for second receipt");
  }
}

async function validateNoUnsupportedActiveMarkCodes(orderId: number): Promise<void> {
  const unsupportedMarks = await prisma.markCode.findMany({
    where: {
      isActive: true,
      shipmentProduct: {
        isActive: true,
        shipment: { orderId }
      },
      OR: [
        { orderItemId: null },
        { orderItem: { isActive: false } },
        { orderItem: { isMarked: false } }
      ]
    },
    select: {
      id: true,
      code: true,
      orderItemId: true,
      orderItem: {
        select: {
          clientCode: true,
          isActive: true,
          isMarked: true
        }
      },
      shipmentProduct: {
        select: {
          clientCode: true
        }
      }
    },
    take: 20
  });

  if (unsupportedMarks.length === 0) {
    return;
  }

  throw new Error(
    `Active KIZ codes are not ready for fiscal receipt: ${unsupportedMarks
      .map((markCode) => {
        const item = markCode.orderItem
          ? `${markCode.orderItem.clientCode} active=${markCode.orderItem.isActive} marked=${markCode.orderItem.isMarked}`
          : "unlinked";
        return `${markCode.id}/${markCode.shipmentProduct.clientCode}/${item}`;
      })
      .join("; ")}`
  );
}

function validateOrderHasActiveItems(order: { items: unknown[] }): void {
  if (order.items.length === 0) {
    throw new Error("Cannot create fiscal receipt for order with no active items");
  }
}

function validateMarkedItemsReady(
  order: Awaited<ReturnType<typeof prisma.order.findUniqueOrThrow>> & {
    items: Array<{ id: number; quantity: number; isMarked: boolean; clientCode: string }>;
  },
  markCodes: Array<{ id: number; orderItemId: number | null; orderItem?: { isActive: boolean } | null }>
): void {
  const unlinked = markCodes.filter(
    (markCode) => !markCode.orderItemId || !markCode.orderItem || markCode.orderItem.isActive === false
  );
  if (unlinked.length > 0) {
    throw new Error(`Active KIZ codes are not linked to active order items: ${unlinked.map((markCode) => markCode.id).join(", ")}`);
  }

  const codesByItemId = new Map<number, number>();
  for (const markCode of markCodes) {
    if (!markCode.orderItemId) {
      continue;
    }

    codesByItemId.set(markCode.orderItemId, (codesByItemId.get(markCode.orderItemId) ?? 0) + 1);
  }

  const missing = order.items.filter((item) => {
    return item.isMarked && (codesByItemId.get(item.id) ?? 0) < item.quantity;
  });

  if (missing.length > 0) {
    throw new Error(
      `Not enough KIZ codes for marked order items: ${missing
        .map((item) => `${item.clientCode} need ${item.quantity}, got ${codesByItemId.get(item.id) ?? 0}`)
        .join("; ")}`
    );
  }

  const excess = order.items.filter((item) => {
    return item.isMarked && (codesByItemId.get(item.id) ?? 0) > item.quantity;
  });

  if (excess.length > 0) {
    throw new Error(
      `Too many KIZ codes for marked order items after returns: ${excess
        .map((item) => `${item.clientCode} need ${item.quantity}, got ${codesByItemId.get(item.id) ?? 0}`)
        .join("; ")}`
    );
  }
}

function shouldMarkCodesUsed(receipt: { status: string }): boolean {
  return ["dry_run", "sent", "queued", "processed"].includes(receipt.status);
}

function isSendingTimedOut(updatedAt: Date): boolean {
  return Date.now() - updatedAt.getTime() > config.FISCAL_RECEIPT_SENDING_TIMEOUT_MS;
}

function extractReceiptId(model: unknown): string | undefined {
  if (!model) {
    return undefined;
  }

  if (typeof model === "string") {
    const id = model.trim();
    return id.length > 0 ? id : undefined;
  }

  if (typeof model !== "object") {
    return undefined;
  }

  const id = (model as { Id?: unknown }).Id;
  if (typeof id !== "string") {
    return undefined;
  }

  const normalizedId = id.trim();
  return normalizedId.length > 0 ? normalizedId : undefined;
}

function normalizeCloudKassirStatus(model: unknown): string {
  if (typeof model !== "string") {
    return "unknown";
  }

  switch (model) {
    case "Processed":
      return "processed";
    case "Queued":
      return "queued";
    case "Error":
      return "error";
    case "NotFound":
      return "not_found";
    default:
      return model.toLowerCase();
  }
}

async function loadProcessedIncomeReturnReceipts(orderExternalId: string): Promise<ProcessedReturnReceipt[]> {
  return prisma.cloudKassirReceipt.findMany({
    where: {
      invoiceId: orderExternalId,
      type: "IncomeReturn",
      OR: [{ status: "Processed" }, { status: "processed" }]
    },
    select: {
      cloudKassirId: true,
      amountKop: true,
      dateTime: true,
      importedAt: true,
      items: {
        select: {
          label: true,
          priceKop: true,
          quantity: true,
          amountKop: true
        }
      }
    },
    orderBy: [{ dateTime: "asc" }, { importedAt: "asc" }]
  });
}

function adjustOrderForPreSecondReturns(order: FiscalOrder, returnReceipts: ProcessedReturnReceipt[]): FiscalOrder | null {
  if (returnReceipts.length === 0) {
    return order;
  }

  const returnAmountKop = returnReceipts.reduce((sum, receipt) => sum + receiptAmountKop(receipt), 0);
  if (returnAmountKop >= order.amountKop - 1) {
    return null;
  }

  const adjustedItems = order.items.map((item) => ({ ...item }));

  for (const receipt of returnReceipts) {
    for (const returnItem of receipt.items) {
      const label = normalizeLabel(returnItem.label ?? "");
      const amountKop = returnItem.amountKop ?? 0;
      if (!label || amountKop <= 0) {
        continue;
      }

      const quantity = parsePositiveQuantity(returnItem.quantity);
      const item = adjustedItems.find((candidate) => canApplyPreSecondReturn(candidate, label, quantity, amountKop));
      if (!item) {
        throw new Error(
          `Cannot match pre-shipment return item "${returnItem.label}" from receipt ${receipt.cloudKassirId} to active order items`
        );
      }

      applyPreSecondReturn(item, quantity, amountKop);
    }
  }

  const activeItems = adjustedItems.filter((item) => item.quantity > 0 && item.amountKop > 0);
  const adjustedAmountKop = activeItems.reduce((sum, item) => sum + item.amountKop, 0);
  const expectedAmountKop = order.amountKop - returnAmountKop;

  if (Math.abs(adjustedAmountKop - expectedAmountKop) > 1) {
    throw new Error(
      `Pre-shipment return amount mismatch: order ${order.amountKop} kop, returns ${returnAmountKop} kop, adjusted items ${adjustedAmountKop} kop`
    );
  }

  if (activeItems.length === 0) {
    return null;
  }

  return {
    ...order,
    amountKop: adjustedAmountKop,
    items: activeItems
  };
}

function isProcessedImportedReceiptStatus(status: string): boolean {
  return status.toLowerCase() === "processed";
}

function receiptAmountKop(receipt: { amountKop: number | null; items: Array<{ amountKop: number | null }> }): number {
  if (receipt.amountKop !== null) {
    return receipt.amountKop;
  }

  return receipt.items.reduce((sum, item) => sum + (item.amountKop ?? 0), 0);
}

function canApplyPreSecondReturn(item: FiscalOrderItem, normalizedReturnLabel: string, quantity: number, amountKop: number): boolean {
  if (item.quantity <= 0 || item.amountKop <= 0 || normalizeLabel(item.name) !== normalizedReturnLabel) {
    return false;
  }

  if (item.isMarked) {
    return Number.isInteger(quantity) && quantity > 0 && quantity <= item.quantity && Math.abs(item.unitPriceKop * quantity - amountKop) <= 1;
  }

  if (Number.isInteger(quantity) && quantity > 0 && quantity <= item.quantity && Math.abs(item.unitPriceKop * quantity - amountKop) <= 1) {
    return true;
  }

  return item.quantity === 1 && amountKop < item.amountKop;
}

function applyPreSecondReturn(item: FiscalOrderItem, quantity: number, amountKop: number): void {
  if (Number.isInteger(quantity) && quantity > 0 && quantity <= item.quantity && Math.abs(item.unitPriceKop * quantity - amountKop) <= 1) {
    item.quantity -= quantity;
    item.amountKop = item.unitPriceKop * item.quantity;
    return;
  }

  if (!item.isMarked && item.quantity === 1 && amountKop < item.amountKop) {
    item.amountKop -= amountKop;
    item.unitPriceKop = item.amountKop;
    return;
  }

  throw new Error(`Unsupported pre-shipment return split for order item ${item.clientCode}`);
}

function parsePositiveQuantity(quantity: string | null): number {
  if (!quantity) {
    return 1;
  }

  const parsed = Number(quantity);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
}

function normalizeLabel(label: string): string {
  return label.trim().replace(/\s+/g, " ").toLowerCase();
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
