import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { prisma } from "../db.js";
import { ReceiptService } from "../services/receipt-service.js";
import { isAdminAuthorized } from "./auth.js";

const receiptListQuerySchema = z.object({
  status: z.string().optional(),
  kind: z.string().optional()
});

export async function registerReceiptRoutes(app: FastifyInstance) {
  const receiptService = new ReceiptService();

  app.get("/api/receipts", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.code(401).send({ error: "Unauthorized" });
    }

    const query = receiptListQuerySchema.parse(request.query);
    return prisma.fiscalReceipt.findMany({
      where: {
        status: query.status,
        kind: query.kind
      },
      orderBy: { createdAt: "desc" },
      take: 100
    });
  });

  app.post("/api/receipts/:id/refresh-status", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.code(401).send({ error: "Unauthorized" });
    }

    const { id } = request.params as { id: string };
    const receiptId = Number(id);
    if (!Number.isInteger(receiptId) || receiptId <= 0) {
      return reply.code(400).send({ error: "Invalid receipt id" });
    }

    return receiptService.refreshCloudKassirStatus(receiptId);
  });
}
