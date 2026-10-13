import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { prisma } from "../db.js";
import { CloudKassirImportService } from "../services/cloudkassir-import-service.js";
import { OrderReconstructionService } from "../services/order-reconstruction-service.js";
import { basicAuthChallenge, isAdminAuthorized } from "./auth.js";

const importTransactionsSchema = z.object({
  from: z.string().min(1),
  to: z.string().min(1),
  timeZone: z.string().default("MSK"),
  maxPages: z.number().int().positive().max(1000).default(100),
  statuses: z.array(z.enum(["Authorized", "Completed", "Cancelled", "Declined"])).optional()
});

const importReceiptsSchema = z.object({
  ids: z.array(z.string().min(1)).min(1)
});

const importReceiptsPeriodSchema = z.object({
  from: z.string().min(1),
  to: z.string().min(1),
  limit: z.number().int().positive().max(500).default(100),
  maxPages: z.number().int().positive().max(1000).default(100)
});

const reconstructOrdersSchema = z.object({
  take: z.number().int().positive().max(200).default(50),
  from: z.string().optional(),
  to: z.string().optional()
});

export async function registerCloudKassirImportRoutes(app: FastifyInstance) {
  const importService = new CloudKassirImportService();
  const orderReconstructionService = new OrderReconstructionService();

  app.post("/api/cloudkassir/import/transactions", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const input = importTransactionsSchema.parse(request.body);
    const result = await importService.importTransactionsByPeriod(input);
    return reply.code(200).send(result);
  });

  app.post("/api/cloudkassir/import/receipts", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const input = importReceiptsSchema.parse(request.body);
    const result = await importService.importReceiptsByIds(input.ids);
    return reply.code(200).send(result);
  });

  app.post("/api/cloudkassir/import/receipts-period", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const input = importReceiptsPeriodSchema.parse(request.body);
    const result = await importService.importReceiptsByFiscalPeriod(input);
    return reply.code(200).send(result);
  });

  app.post("/api/cloudkassir/reconstruct-orders", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const input = reconstructOrdersSchema.parse(request.body ?? {});
    const result = await orderReconstructionService.reconstructPendingOrders({
      take: input.take,
      receiptDateFrom: input.from,
      receiptDateTo: input.to
    });
    return reply.code(200).send(result);
  });

  app.get("/api/cloudkassir/transactions", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    return prisma.cloudPaymentTransaction.findMany({
      orderBy: { importedAt: "desc" },
      take: 200
    });
  });

  app.get("/api/cloudkassir/receipts", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    return prisma.cloudKassirReceipt.findMany({
      include: { items: true },
      orderBy: { importedAt: "desc" },
      take: 200
    });
  });

  app.get("/api/cloudkassir/snapshot", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    return importService.fetchCashRegisterSnapshot();
  });
}
