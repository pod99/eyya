import { FfPlatformClient } from "../clients/ff-platform.js";
import { prisma } from "../db.js";
import { stringifyJson } from "../utils/json.js";

export class OrderExternalIdNotFoundError extends Error {
  constructor(orderExternalId: string) {
    super(`Order not found for orderExternalId: ${orderExternalId}`);
    this.name = "OrderExternalIdNotFoundError";
  }
}

export class FfSyncService {
  constructor(private readonly ffClient = new FfPlatformClient()) {}

  async syncSkladDispatch(skladDispatchId: number, orderExternalId?: string) {
    const [skladDispatch, cdekDetail] = await Promise.all([
      this.ffClient.getSkladDispatch(skladDispatchId),
      this.ffClient.getCdekDispatchDetail(skladDispatchId)
    ]);

    if (!cdekDetail) {
      throw new Error(`CDEK detail not found for skladDispatchId: ${skladDispatchId}`);
    }

    const trackNumber = skladDispatch.delivery_performer_track_number ?? cdekDetail.otgruzka.number_contract ?? null;
    const order = await findOrderForShipment(skladDispatchId, trackNumber, orderExternalId);

    if (orderExternalId && !order) {
      throw new OrderExternalIdNotFoundError(orderExternalId);
    }

    return prisma.$transaction(async (tx) => {
      const shipment = await tx.shipment.upsert({
        where: { skladDispatchId },
        create: {
          orderId: order?.id,
          skladDispatchId,
          shopId: skladDispatch.shopId,
          skladId: skladDispatch.skladId,
          status: skladDispatch.status,
          cdekStatus: cdekDetail.otgruzka.status,
          dateCreated: skladDispatch.date_created,
          dateAtWork: skladDispatch.date_at_work,
          dateAtShipped: skladDispatch.date_at_otgrugen,
          dateClosed: cdekDetail.otgruzka.dateClosed,
          trackNumber,
          rawSkladDispatchJson: stringifyJson(skladDispatch),
          rawCdekDetailJson: stringifyJson(cdekDetail)
        },
        update: {
          orderId: order?.id,
          shopId: skladDispatch.shopId,
          skladId: skladDispatch.skladId,
          status: skladDispatch.status,
          cdekStatus: cdekDetail.otgruzka.status,
          dateAtWork: skladDispatch.date_at_work,
          dateAtShipped: skladDispatch.date_at_otgrugen,
          dateClosed: cdekDetail.otgruzka.dateClosed,
          trackNumber,
          rawSkladDispatchJson: stringifyJson(skladDispatch),
          rawCdekDetailJson: stringifyJson(cdekDetail)
        }
      });

      await tx.shipmentProduct.updateMany({
        where: { shipmentId: shipment.id },
        data: { isActive: false }
      });

      for (const product of cdekDetail.products) {
        const shipmentProduct = await tx.shipmentProduct.upsert({
          where: {
            shipmentId_clientCode: {
              shipmentId: shipment.id,
              clientCode: product.client_code
            }
          },
          create: {
            shipmentId: shipment.id,
            ffProductId: product.id,
            clientCode: product.client_code,
            name: product.name,
            planQty: product.plan_qty,
            factQty: product.fact_qty,
            isActive: true
          },
          update: {
            ffProductId: product.id,
            name: product.name,
            planQty: product.plan_qty,
            factQty: product.fact_qty,
            isActive: true
          }
        });

        let orderItem = null;
        if (order) {
          orderItem = await tx.orderItem.findFirst({
            where: {
              orderId: order.id,
              isActive: true,
              clientCode: product.client_code
            }
          });

          if (!orderItem) {
            orderItem = await tx.orderItem.findFirst({
              where: {
                orderId: order.id,
                isActive: true,
                ffProductId: product.id
              }
            });
          }
        }

        const activeCodes = normalizeKizCodes(product.kiz_codes);
        await tx.markCode.updateMany({
          where: {
            shipmentProductId: shipmentProduct.id,
            code: { notIn: activeCodes }
          },
          data: { isActive: false }
        });

        for (const code of activeCodes) {
          await tx.markCode.upsert({
            where: {
              shipmentProductId_code: {
                shipmentProductId: shipmentProduct.id,
                code
              }
            },
            create: {
              shipmentProductId: shipmentProduct.id,
              orderItemId: orderItem?.id,
              code,
              isActive: true
            },
            update: {
              ...(order ? { orderItemId: orderItem?.id ?? null } : {}),
              isActive: true
            }
          });
        }
      }

      if (order) {
        await tx.order.update({
          where: { id: order.id },
          data: {
            skladDispatchId,
            deliveryTrackNumber: shipment.trackNumber
          }
        });
      }

      return tx.shipment.findUniqueOrThrow({
        where: { id: shipment.id },
        include: {
          products: {
            include: { markCodes: true }
          }
        }
      });
    });
  }
}

async function findOrderForShipment(skladDispatchId: number, trackNumber: string | null, orderExternalId?: string) {
  if (orderExternalId) {
    return prisma.order.findUnique({ where: { externalId: orderExternalId } });
  }

  return prisma.order.findFirst({
    where: {
      OR: [
        { skladDispatchId },
        ...(trackNumber ? [{ deliveryTrackNumber: trackNumber }] : [])
      ]
    }
  });
}

function normalizeKizCodes(kizCodes: unknown): string[] {
  if (!Array.isArray(kizCodes)) {
    return [];
  }

  return [
    ...new Set(
      kizCodes
        .filter((code): code is string => typeof code === "string")
        .map((code) => code.trim().replace(/^\u001D+/, ""))
        .filter((code) => code.length > 0)
    )
  ];
}
