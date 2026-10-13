import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { config } from "../config.js";
import { prisma } from "../db.js";
import { CloudKassirImportService } from "../services/cloudkassir-import-service.js";
import { stringifyJson } from "../utils/json.js";
import { timingSafeStringEqual } from "./auth.js";

export async function registerWebhookRoutes(app: FastifyInstance) {
  const cloudKassirImportService = new CloudKassirImportService();

  const handleCloudKassirWebhook = async (request: FastifyRequest, reply: FastifyReply) => {
    const params = request.params as { event?: string; secret?: string };
    if (!isWebhookAuthorized(request, params.secret)) {
      return reply.code(401).send({ error: "Unauthorized webhook" });
    }

    const eventType = normalizeWebhookEvent(params.event);
    const payload = normalizeReceiptWebhookPayload(
      request.method === "GET" ? (request.query as Record<string, unknown>) : (request.body as Record<string, unknown>)
    );
    const externalId = readStringOrNumber(payload, ["InvoiceId", "invoiceId", "TransactionId", "transactionId", "Id", "id"]);

    await prisma.apiEvent.create({
      data: {
        source: "cloudkassir",
        eventType,
        externalId,
        payloadJson: stringifyJson(payload)
      }
    });

    if (eventType === "receipt") {
      await cloudKassirImportService.importReceiptWebhook(payload);
    } else {
      await upsertCloudPaymentTransactionFromWebhook(payload);
    }

    return reply.code(200).send({ code: 0 });
  };

  app.route({ method: ["GET", "POST"], url: "/webhooks/cloudkassir/:event", handler: handleCloudKassirWebhook });
  app.route({ method: ["GET", "POST"], url: "/webhooks/cloudkassir/:event/:secret", handler: handleCloudKassirWebhook });
}

function isWebhookAuthorized(request: FastifyRequest, pathSecret?: string): boolean {
  if (!config.CLOUDKASSIR_WEBHOOK_SECRET) {
    return true;
  }

  if (timingSafeStringEqual(pathSecret, config.CLOUDKASSIR_WEBHOOK_SECRET)) {
    return true;
  }

  const headerSecret = request.headers["x-webhook-secret"];
  const providedSecret = Array.isArray(headerSecret) ? headerSecret[0] : headerSecret;

  return timingSafeStringEqual(providedSecret, config.CLOUDKASSIR_WEBHOOK_SECRET);
}

function normalizeReceiptWebhookPayload(payload: Record<string, unknown> | undefined): Record<string, unknown> {
  const normalized = { ...(payload ?? {}) };
  for (const key of ["Receipt", "CustomerReceipt"]) {
    const value = normalized[key];
    if (typeof value !== "string") {
      continue;
    }

    try {
      normalized[key] = JSON.parse(value);
    } catch {
      normalized[key] = value;
    }
  }

  return normalized;
}

function normalizeWebhookEvent(event: string | undefined): string {
  const normalized = (event ?? "unknown").trim().toLowerCase();
  return /^[a-z0-9_-]+$/.test(normalized) ? normalized : "unknown";
}

async function upsertCloudPaymentTransactionFromWebhook(payload: Record<string, unknown>): Promise<void> {
  const transactionId = readStringOrNumber(payload, ["TransactionId", "transactionId"]);
  if (!transactionId) {
    return;
  }

  await prisma.cloudPaymentTransaction.upsert({
    where: { transactionId },
    create: {
      transactionId,
      invoiceId: readStringOrNumber(payload, ["InvoiceId", "invoiceId"]),
      accountId: readStringOrNumber(payload, ["AccountId", "accountId"]),
      email: readStringOrNumber(payload, ["Email", "email"]),
      amountKop: readRubAsKop(payload, ["Amount", "PaymentAmount", "amount"]),
      currency: readStringOrNumber(payload, ["Currency", "currency"]),
      operationType: readStringOrNumber(payload, ["OperationType", "Type", "type"]),
      status: readStringOrNumber(payload, ["Status", "status"]),
      statusCode: readNumber(payload, ["StatusCode", "statusCode"]),
      reason: readStringOrNumber(payload, ["Reason", "reason"]),
      reasonCode: readNumber(payload, ["ReasonCode", "reasonCode"]),
      cardFirstSix: readStringOrNumber(payload, ["CardFirstSix", "cardFirstSix"]),
      cardLastFour: readStringOrNumber(payload, ["CardLastFour", "cardLastFour"]),
      cardType: readStringOrNumber(payload, ["CardType", "cardType"]),
      paymentDate: readStringOrNumber(payload, ["DateTime", "CreatedDateIso", "AuthDateIso", "ConfirmDateIso"]),
      rawJson: stringifyJson(payload)
    },
    update: {
      invoiceId: readStringOrNumber(payload, ["InvoiceId", "invoiceId"]),
      accountId: readStringOrNumber(payload, ["AccountId", "accountId"]),
      email: readStringOrNumber(payload, ["Email", "email"]),
      amountKop: readRubAsKop(payload, ["Amount", "PaymentAmount", "amount"]),
      currency: readStringOrNumber(payload, ["Currency", "currency"]),
      operationType: readStringOrNumber(payload, ["OperationType", "Type", "type"]),
      status: readStringOrNumber(payload, ["Status", "status"]),
      statusCode: readNumber(payload, ["StatusCode", "statusCode"]),
      reason: readStringOrNumber(payload, ["Reason", "reason"]),
      reasonCode: readNumber(payload, ["ReasonCode", "reasonCode"]),
      cardFirstSix: readStringOrNumber(payload, ["CardFirstSix", "cardFirstSix"]),
      cardLastFour: readStringOrNumber(payload, ["CardLastFour", "cardLastFour"]),
      cardType: readStringOrNumber(payload, ["CardType", "cardType"]),
      paymentDate: readStringOrNumber(payload, ["DateTime", "CreatedDateIso", "AuthDateIso", "ConfirmDateIso"]),
      rawJson: stringifyJson(payload)
    }
  });
}

function readStringOrNumber(payload: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
    if (typeof value === "number") {
      return String(value);
    }
  }

  return undefined;
}

function readNumber(payload: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number") {
      return value;
    }
    if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }

  return undefined;
}

function readRubAsKop(payload: Record<string, unknown>, keys: string[]): number | undefined {
  const value = readNumber(payload, keys);
  return value === undefined ? undefined : Math.round(value * 100);
}
