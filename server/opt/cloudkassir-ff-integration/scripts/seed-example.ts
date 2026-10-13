import { upsertOrder } from "../src/services/order-service.js";
import { prisma } from "../src/db.js";

const order = await upsertOrder({
  externalId: "ORDER-330135",
  accountId: "demo-user",
  customerEmail: "buyer@example.ru",
  paymentStatus: "paid",
  skladDispatchId: 330135,
  items: [
    {
      ffProductId: 38909,
      clientCode: "EY_04_skinrise",
      name: "Exo System Box. Система ухода за кожей",
      quantity: 1,
      unitPriceRub: 1000,
      vat: 22,
      isMarked: true
    },
    {
      ffProductId: 38906,
      clientCode: "EY_07_tula",
      name: "Tula. Блеск-бальзам для губ с экзосомами и пептидами",
      quantity: 1,
      unitPriceRub: 700,
      vat: 22,
      isMarked: true
    }
  ]
});

const shipment = await prisma.shipment.upsert({
  where: { skladDispatchId: 330135 },
  create: {
    orderId: order.id,
    skladDispatchId: 330135,
    shopId: 144,
    skladId: 2,
    status: "otgrugen",
    cdekStatus: "sended",
    dateCreated: "01.06.2026 14:59:34",
    dateAtShipped: "01.06.2026 16:51:21",
    trackNumber: "10273834123"
  },
  update: {
    orderId: order.id,
    status: "otgrugen",
    cdekStatus: "sended",
    trackNumber: "10273834123"
  }
});

await prisma.shipmentProduct.deleteMany({
  where: { shipmentId: shipment.id }
});

const products = [
  {
    ffProductId: 38909,
    clientCode: "EY_04_skinrise",
    name: "Exo System Box. Система ухода за кожей",
    planQty: 1,
    factQty: 1,
    code: "0104624490935039215;g!y.\u001D91EE12\u001D92AZ4ApjfsQXTwuxA2LyDeUIKRfAaG48Y3afJ11Bp8Gy8="
  },
  {
    ffProductId: 38906,
    clientCode: "EY_07_tula",
    name: "Tula. Блеск-бальзам для губ с экзосомами и пептидами",
    planQty: 1,
    factQty: 1,
    code: "\u001D0104624490935060215k5C)p\u001D91EE11\u001D92934u4MaWK8FVOeo3o/oeC95oRW7HsbxflBAkRqpVNYw="
  }
];

for (const product of products) {
  const shipmentProduct = await prisma.shipmentProduct.create({
    data: {
      shipmentId: shipment.id,
      ffProductId: product.ffProductId,
      clientCode: product.clientCode,
      name: product.name,
      planQty: product.planQty,
      factQty: product.factQty
    }
  });

  const orderItem = await prisma.orderItem.findFirstOrThrow({
    where: {
      orderId: order.id,
      clientCode: product.clientCode
    }
  });

  await prisma.markCode.create({
    data: {
      shipmentProductId: shipmentProduct.id,
      orderItemId: orderItem.id,
      code: product.code
    }
  });
}

console.log(`Seeded ${order.externalId}`);
