import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { FfSyncService, OrderExternalIdNotFoundError } from "../services/ff-sync-service.js";

const syncQuerySchema = z.object({
  orderExternalId: z.string().optional()
});

export async function registerFfRoutes(app: FastifyInstance) {
  const ffSyncService = new FfSyncService();

  app.post("/api/ff/sklad-dispatch/:id/sync", async (request, reply) => {
    const { id } = request.params as { id: string };
    const skladDispatchId = Number(id);
    if (!Number.isInteger(skladDispatchId) || skladDispatchId <= 0) {
      return reply.code(400).send({ error: "Invalid sklad dispatch id" });
    }

    const query = syncQuerySchema.parse(request.query);
    try {
      const shipment = await ffSyncService.syncSkladDispatch(skladDispatchId, query.orderExternalId);
      return reply.code(200).send(shipment);
    } catch (error) {
      if (error instanceof OrderExternalIdNotFoundError) {
        return reply.code(400).send({ error: error.message });
      }

      throw error;
    }
  });
}
