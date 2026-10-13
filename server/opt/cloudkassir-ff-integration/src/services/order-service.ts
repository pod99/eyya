import { z } from "zod";
import { prisma } from "../db.js";
import { rubToKop } from "../utils/money.js";

export const createOrderSchema = z
  .object({
    externalId: z.string().min(1),
    accountId: z.string().optional(),
    customerEmail: z.string().email().optional(),
    customerPhone: z.string().optional(),
    paymentStatus: z.string().default("paid"),
    skladDispatchId: z.number().int().positive().optional(),
    deliveryTrackNumber: z.string().min(1).optional(),
    items: z.array(
      z.object({
        ffProductId: z.number().int().positive().optional(),
        clientCode: z.string().min(1),
        name: z.string().min(1),
        quantity: z.number().int().positive(),
        unitPriceRub: z.number().positive(),
        vat: z.number().int().nullable().optional(),
        isMarked: z.boolean().default(false),
        objectCodeBeforeMark: z.number().int().positive().optional(),
        objectCodeWithMark: z.number().int().positive().optional()
      })
    )
  })
  .refine(
    (input) => new Set(input.items.map((item) => item.clientCode)).size === input.items.length,
    "Order items must have unique clientCode values"
  );

export type CreateOrderInput = z.infer<typeof createOrderSchema>;

export async function upsertOrder(input: CreateOrderInput) {
  const amountKop = input.items.reduce((sum, item) => {
    return sum + rubToKop(item.unitPriceRub) * item.quantity;
  }, 0);

  return prisma.$transaction(async (tx) => {
    const order = await tx.order.upsert({
      where: { externalId: input.externalId },
      create: {
        externalId: input.externalId,
        accountId: input.accountId,
        customerEmail: input.customerEmail,
        customerPhone: input.customerPhone,
        paymentStatus: input.paymentStatus,
        skladDispatchId: input.skladDispatchId,
        deliveryTrackNumber: input.deliveryTrackNumber,
        amountKop
      },
      update: {
        accountId: input.accountId,
        customerEmail: input.customerEmail,
        customerPhone: input.customerPhone,
        paymentStatus: input.paymentStatus,
        skladDispatchId: input.skladDispatchId,
        deliveryTrackNumber: input.deliveryTrackNumber,
        amountKop
      }
    });

    const activeClientCodes = input.items.map((item) => item.clientCode);
    await tx.orderItem.updateMany({
      where: {
        orderId: order.id,
        clientCode: { notIn: activeClientCodes }
      },
      data: { isActive: false }
    });

    for (const item of input.items) {
      const unitPriceKop = rubToKop(item.unitPriceRub);
      const objectCodeBeforeMark = item.objectCodeBeforeMark ?? (item.isMarked ? 32 : 1);
      const objectCodeWithMark = item.objectCodeWithMark ?? 33;
      await tx.orderItem.upsert({
        where: {
          orderId_clientCode: {
            orderId: order.id,
            clientCode: item.clientCode
          }
        },
        create: {
          orderId: order.id,
          ffProductId: item.ffProductId,
          clientCode: item.clientCode,
          name: item.name,
          quantity: item.quantity,
          unitPriceKop,
          amountKop: unitPriceKop * item.quantity,
          vat: item.vat,
          isMarked: item.isMarked,
          objectCodeBeforeMark,
          objectCodeWithMark,
          isActive: true
        },
        update: {
          ffProductId: item.ffProductId,
          name: item.name,
          quantity: item.quantity,
          unitPriceKop,
          amountKop: unitPriceKop * item.quantity,
          vat: item.vat,
          isMarked: item.isMarked,
          objectCodeBeforeMark,
          objectCodeWithMark,
          isActive: true
        }
      });
    }

    return tx.order.findUniqueOrThrow({
      where: { id: order.id },
      include: { items: { where: { isActive: true } }, receipts: true, shipments: true }
    });
  });
}
