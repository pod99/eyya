import type { FastifyInstance } from "fastify";
import { prisma } from "../db.js";
import { createOrderSchema, upsertOrder } from "../services/order-service.js";
import { ReceiptService } from "../services/receipt-service.js";
import { basicAuthChallenge, isAdminAuthorized } from "./auth.js";

export async function registerOrderRoutes(app: FastifyInstance) {
  const receiptService = new ReceiptService();

  app.post("/api/orders", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const input = createOrderSchema.parse(request.body);
    const order = await upsertOrder(input);
    return reply.code(201).send(order);
  });

  app.get("/api/orders/:externalId", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.code(401).send({ error: "Unauthorized" });
    }

    const { externalId } = request.params as { externalId: string };
    return prisma.order.findUniqueOrThrow({
      where: { externalId },
      include: {
        items: true,
        shipments: {
          include: {
            products: {
              include: { markCodes: true }
            }
          }
        },
        receipts: true
      }
    });
  });

  app.post("/api/orders/:externalId/receipts/first", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const { externalId } = request.params as { externalId: string };
    const receipt = await receiptService.createFirstPrepaymentReceipt(externalId);
    return reply.code(201).send(receipt);
  });

  app.post("/api/orders/:externalId/receipts/second", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.header("WWW-Authenticate", basicAuthChallenge()).code(401).send({ error: "Unauthorized" });
    }

    const { externalId } = request.params as { externalId: string };
    const receipt = await receiptService.createSecondFullPayReceipt(externalId);
    return reply.code(201).send(receipt);
  });
}
