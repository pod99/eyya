import type { Prisma } from "@prisma/client";
import { config } from "../config.js";
import { CdekClient, CdekHttpError, type CdekOrderEntity } from "../clients/cdek.js";
import { prisma } from "../db.js";
import { stringifyJson } from "../utils/json.js";

const CDEK_NOT_FOUND_RETRY_MS = 24 * 60 * 60 * 1000;

export class CdekEnrichmentService {
  constructor(private readonly cdekClient = new CdekClient()) {}

  async enrichPendingShipments() {
    const notFoundRetryAfter = new Date(Date.now() - CDEK_NOT_FOUND_RETRY_MS);
    const recentNotFoundTrackNumbers = await recentCdekNotFoundTrackNumbers(notFoundRetryAfter);
    const trackNumberFilter = recentNotFoundTrackNumbers.length > 0
      ? { not: null, notIn: recentNotFoundTrackNumbers }
      : { not: null };
    const shipments = await prisma.shipment.findMany({
      where: {
        trackNumber: trackNumberFilter,
        OR: [
          { rawCdekOrderJson: null },
          { cdekRecipientPhone: null },
          { orderId: null },
          { cdekDeliveryStatus: null },
          { cdekDeliveryStatus: { not: "DELIVERED" } }
        ]
      },
      orderBy: { updatedAt: "asc" },
      take: config.CDEK_ENRICH_BATCH_SIZE
    });

    let enriched = 0;
    let linked = 0;
    let errors = 0;

    for (const shipment of shipments) {
      const trackNumber = shipment.trackNumber;
      if (!trackNumber) {
        continue;
      }

      try {
        const response = await this.cdekClient.getOrderByCdekNumber(trackNumber);
        const entity = response.entity;
        if (!entity) {
          continue;
        }

        const orderId = await findMatchingOrderId(entity, trackNumber, shipment.skladDispatchId);
        await prisma.shipment.update({
          where: { id: shipment.id },
          data: {
            orderId: orderId ?? shipment.orderId,
            cdekUuid: entity.uuid,
            cdekNumber: entity.cdek_number ?? trackNumber,
            cdekOrderNumber: entity.number,
            cdekDeliveryStatus: latestCdekStatusCode(entity) ?? undefined,
            cdekRecipientName: entity.recipient?.name ?? entity.recipient?.company,
            cdekRecipientPhone: normalizePhone(entity.recipient?.phones?.[0]?.number),
            cdekRecipientEmail: entity.recipient?.email,
            rawCdekOrderJson: stringifyJson(response)
          }
        });

        if (orderId) {
          await updateOrderFromCdek(orderId, entity, trackNumber, shipment.skladDispatchId);
          linked += shipment.orderId === orderId ? 0 : 1;
        }

        enriched += 1;
      } catch (error) {
        errors += 1;
        if (isCdekOrderNotFound(error)) {
          await markCdekOrderNotFound(shipment.id, trackNumber, error);
        }
        await recordCdekError(trackNumber, error);
      }
    }

    return {
      requested: shipments.length,
      enriched,
      linked,
      errors
    };
  }
}

async function recentCdekNotFoundTrackNumbers(since: Date): Promise<string[]> {
  const rows = await prisma.apiEvent.findMany({
    where: {
      source: "cdek",
      eventType: "enrich_error",
      externalId: { not: null },
      createdAt: { gt: since },
      payloadJson: { contains: "v2_order_not_found" }
    },
    select: { externalId: true },
    orderBy: { createdAt: "desc" },
    take: 1000
  });

  return [...new Set(rows.map((row) => row.externalId).filter((value): value is string => Boolean(value)))];
}

async function findMatchingOrderId(entity: CdekOrderEntity, trackNumber: string, skladDispatchId: number): Promise<number | null> {
  const phone = normalizePhone(entity.recipient?.phones?.[0]?.number);
  const orderNumber = entity.number ?? undefined;
  const cdekNumber = entity.cdek_number ?? trackNumber;

  const strongFilters: Prisma.OrderWhereInput[] = [
    { skladDispatchId },
    { deliveryTrackNumber: cdekNumber },
    { deliveryTrackNumber: trackNumber },
    ...(orderNumber ? [{ externalId: orderNumber }] : [])
  ];

  for (const filter of strongFilters) {
    const order = await prisma.order.findFirst({
      where: filter,
      orderBy: { updatedAt: "desc" },
      select: { id: true }
    });
    if (order) {
      return order.id;
    }
  }

  if (!phone) {
    return null;
  }

  const phoneCandidates = await prisma.order.findMany({
    where: {
      customerPhone: { contains: phone.slice(-10) },
      skladDispatchId: null,
      deliveryTrackNumber: null
    },
    orderBy: { updatedAt: "desc" },
    take: 2,
    select: { id: true }
  });

  return phoneCandidates.length === 1 ? phoneCandidates[0].id : null;
}

async function updateOrderFromCdek(orderId: number, entity: CdekOrderEntity, trackNumber: string, skladDispatchId: number) {
  const phone = normalizePhone(entity.recipient?.phones?.[0]?.number);
  await prisma.order.update({
    where: { id: orderId },
    data: {
      skladDispatchId,
      deliveryTrackNumber: entity.cdek_number ?? trackNumber,
      customerPhone: phone ?? undefined,
      customerEmail: entity.recipient?.email ?? undefined
    }
  });
}

function isCdekOrderNotFound(error: unknown): error is CdekHttpError {
  return error instanceof CdekHttpError && error.status === 400 && error.bodyText.includes("v2_order_not_found");
}

async function markCdekOrderNotFound(shipmentId: number, trackNumber: string, error: CdekHttpError) {
  await prisma.shipment.updateMany({
    where: {
      OR: [{ id: shipmentId }, { trackNumber }, { cdekNumber: trackNumber }]
    },
    data: {
      cdekDeliveryStatus: "NOT_FOUND",
      rawCdekOrderJson: stringifyJson({
        source: "cdek",
        eventType: "order_not_found",
        trackNumber,
        status: error.status,
        body: parseMaybeJson(error.bodyText),
        at: new Date().toISOString()
      })
    }
  });
}

function parseMaybeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text.slice(0, 1000) };
  }
}

async function recordCdekError(trackNumber: string, error: unknown) {
  await prisma.apiEvent.create({
    data: {
      source: "cdek",
      eventType: "enrich_error",
      externalId: trackNumber,
      payloadJson: stringifyJson({
        trackNumber,
        message: error instanceof Error ? error.message : String(error),
        name: error instanceof Error ? error.name : undefined,
        status: error instanceof CdekHttpError ? error.status : undefined
      })
    }
  });
}

function normalizePhone(phone: string | null | undefined): string | undefined {
  if (!phone) {
    return undefined;
  }

  const digits = phone.replace(/\D/g, "");
  return digits.length > 0 ? digits : undefined;
}

function latestCdekStatusCode(entity: CdekOrderEntity): string | undefined {
  const statuses = entity.statuses ?? [];
  const latestStatus = statuses[statuses.length - 1];
  return latestStatus?.code ?? undefined;
}
