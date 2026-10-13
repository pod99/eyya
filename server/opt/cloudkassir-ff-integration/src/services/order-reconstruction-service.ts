import type { CloudKassirReceipt, CloudKassirReceiptItem, Shipment, ShipmentProduct, MarkCode } from "@prisma/client";
import { CdekClient, CdekHttpError, type CdekOrderEntity } from "../clients/cdek.js";
import { config } from "../config.js";
import { prisma } from "../db.js";
import { stringifyJson } from "../utils/json.js";
import { kopToRub } from "../utils/money.js";
import { FfSyncService } from "./ff-sync-service.js";
import { upsertOrder, type CreateOrderInput } from "./order-service.js";

type ReceiptWithItems = CloudKassirReceipt & {
  items: CloudKassirReceiptItem[];
};

type ShipmentWithProducts = Shipment & {
  products: Array<ShipmentProduct & { markCodes: MarkCode[] }>;
};

type ReceiptLine = {
  itemId: number;
  label: string;
  normalizedLabel: string;
  quantity: number;
  priceKop: number;
  amountKop: number;
  vat?: number | null;
  objectCode: number;
  isDelivery: boolean;
};

type ProductMatch = {
  line: ReceiptLine;
  product: ShipmentWithProducts["products"][number];
};

type CandidateMatch = {
  shipment: ShipmentWithProducts;
  productMatches: ProductMatch[];
  score: number;
  reasons: string[];
};

type ReconstructPendingOrdersInput = {
  take?: number;
  receiptDateFrom?: string;
  receiptDateTo?: string;
};

const RECONSTRUCTION_SKIP_RETRY_MS = 6 * 60 * 60 * 1000;

export class OrderReconstructionService {
  constructor(
    private readonly ffSyncService = new FfSyncService(),
    private readonly cdekClient = new CdekClient()
  ) {}

  async reconstructPendingOrders(input: number | ReconstructPendingOrdersInput = config.ORDER_RECONSTRUCT_BATCH_SIZE) {
    const options = typeof input === "number" ? { take: input } : input;
    const take = options.take ?? config.ORDER_RECONSTRUCT_BATCH_SIZE;
    const receiptDateFrom = normalizeReceiptDateBound(options.receiptDateFrom);
    const receiptDateTo = normalizeReceiptDateBound(options.receiptDateTo);
    const excludedInvoiceIds = await findReconstructionExcludedInvoiceIds();
    const receipts = await prisma.cloudKassirReceipt.findMany({
      where: {
        invoiceId: excludedInvoiceIds.length > 0 ? { not: null, notIn: excludedInvoiceIds } : { not: null },
        amountKop: { not: null },
        type: { in: ["Income", "income"] },
        ...(receiptDateFrom || receiptDateTo
          ? {
              dateTime: {
                ...(receiptDateFrom ? { gte: receiptDateFrom } : {}),
                ...(receiptDateTo ? { lt: receiptDateTo } : {})
              }
            }
          : {})
      },
      include: { items: true },
      orderBy: { dateTime: "asc" },
      take: Math.max(1, take)
    });

    let inspected = 0;
    let created = 0;
    let skippedExisting = 0;
    let unmatched = 0;
    let ambiguous = 0;
    let errors = 0;

    for (const receipt of receipts) {
      if (inspected >= take) {
        break;
      }

      const invoiceId = receipt.invoiceId;
      if (!invoiceId) {
        continue;
      }

      const existingOrder = await prisma.order.findUnique({
        where: { externalId: invoiceId },
        select: { id: true }
      });
      if (existingOrder) {
        skippedExisting += 1;
        continue;
      }

      inspected += 1;

      try {
        const result = await this.reconstructOne(receipt);
        if (result === "created") {
          created += 1;
        } else if (result === "ambiguous") {
          ambiguous += 1;
        } else {
          unmatched += 1;
        }
      } catch (error) {
        errors += 1;
        await prisma.apiEvent.create({
          data: {
            source: "order_reconstruction",
            eventType: "error",
            externalId: invoiceId,
            payloadJson: stringifyJson({
              cloudKassirId: receipt.cloudKassirId,
              message: error instanceof Error ? error.message : String(error)
            })
          }
        });
      }
    }

    const result = {
      inspected,
      created,
      skippedExisting,
      excludedExistingOrRecent: excludedInvoiceIds.length,
      unmatched,
      ambiguous,
      errors,
      receiptDateFrom,
      receiptDateTo
    };

    if (inspected > 0 || created > 0 || errors > 0) {
      await prisma.apiEvent.create({
        data: {
          source: "order_reconstruction",
          eventType: "batch_result",
          payloadJson: stringifyJson(result)
        }
      });
    }

    return result;
  }

  private async reconstructOne(receipt: ReceiptWithItems): Promise<"created" | "unmatched" | "ambiguous"> {
    const invoiceId = receipt.invoiceId;
    const amountKop = receipt.amountKop;
    if (!invoiceId || amountKop === null) {
      return "unmatched";
    }

    const contacts = extractReceiptContacts(receipt);
    const productLines = extractProductLines(receipt);
    const deliveryLines = extractDeliveryLines(receipt);
    const allLines = [...productLines, ...deliveryLines];

    if (productLines.length === 0 || (!contacts.phoneLast10 && !contacts.email)) {
      await this.recordSkipped(receipt, "missing_contacts_or_products", { contacts, productLines: productLines.length });
      return "unmatched";
    }

    const linesAmountKop = allLines.reduce((sum, line) => sum + line.amountKop, 0);
    if (Math.abs(linesAmountKop - amountKop) > 1) {
      await this.recordSkipped(receipt, "receipt_lines_amount_mismatch", {
        amountKop,
        linesAmountKop
      });
      return "unmatched";
    }

    const cdekShipment = await this.findShipmentByReceiptCdekOrder(receipt);
    const candidates = uniqueShipments([
      ...(cdekShipment ? [cdekShipment] : []),
      ...(await findShipmentCandidates(contacts))
    ]);
    const matches = candidates
      .map((shipment) => scoreShipmentCandidate(receipt, shipment, contacts, productLines))
      .filter((match): match is CandidateMatch => match !== null)
      .sort((a, b) => b.score - a.score);

    if (matches.length === 0) {
      await this.recordSkipped(receipt, "no_unique_contact_product_match", {
        contacts,
        productLines: productLines.map((line) => ({ label: line.label, quantity: line.quantity }))
      });
      return "unmatched";
    }

    const [best, second] = matches;
    if (!best || best.score < 150 || (second && second.score >= best.score - 20)) {
      await this.recordSkipped(receipt, "ambiguous_match", {
        best: best ? matchSummary(best) : null,
        second: second ? matchSummary(second) : null
      });
      return "ambiguous";
    }

    const orderInput = buildOrderInput(receipt, best, contacts, productLines, deliveryLines);
    const order = await upsertOrder(orderInput);
    await this.ffSyncService.syncSkladDispatch(best.shipment.skladDispatchId, order.externalId);
    await linkImportedFirstReceipt(order.id, receipt);

    await prisma.apiEvent.create({
      data: {
        source: "order_reconstruction",
        eventType: "created",
        externalId: invoiceId,
        payloadJson: stringifyJson({
          cloudKassirId: receipt.cloudKassirId,
          skladDispatchId: best.shipment.skladDispatchId,
          trackNumber: best.shipment.trackNumber,
          score: best.score,
          reasons: best.reasons,
          items: orderInput.items.map((item) => ({
            clientCode: item.clientCode,
            name: item.name,
            quantity: item.quantity,
            isMarked: item.isMarked,
            objectCodeBeforeMark: item.objectCodeBeforeMark
          }))
        })
      }
    });

    return "created";
  }

  private async recordSkipped(receipt: ReceiptWithItems, reason: string, details: unknown) {
    await prisma.apiEvent.create({
      data: {
        source: "order_reconstruction",
        eventType: "skipped",
        externalId: receipt.invoiceId,
        payloadJson: stringifyJson({
          reason,
          cloudKassirId: receipt.cloudKassirId,
          details
        })
      }
    });
  }

  private async findShipmentByReceiptCdekOrder(receipt: ReceiptWithItems): Promise<ShipmentWithProducts | null> {
    const imNumber = extractCdekImNumber(receipt.invoiceId);
    if (!imNumber) {
      return null;
    }

    try {
      const response = await this.cdekClient.getOrderByImNumber(imNumber);
      const entity = response.entity;
      const cdekNumber = entity?.cdek_number;
      if (!entity || !cdekNumber) {
        return null;
      }

      await updateShipmentCdekFieldsFromEntity(cdekNumber, entity, response);

      return prisma.shipment.findFirst({
        where: {
          trackNumber: cdekNumber,
          orderId: null
        },
        include: {
          products: {
            where: { isActive: true },
            include: { markCodes: { where: { isActive: true } } }
          }
        }
      });
    } catch (error) {
      if (error instanceof CdekHttpError && error.status === 400) {
        await this.recordSkipped(receipt, "cdek_im_number_not_found", { imNumber });
        return null;
      }

      throw error;
    }
  }
}

async function findReconstructionExcludedInvoiceIds(): Promise<string[]> {
  const retryAfter = new Date(Date.now() - RECONSTRUCTION_SKIP_RETRY_MS);
  const [orders, recentSkips] = await Promise.all([
    prisma.order.findMany({
      select: { externalId: true }
    }),
    prisma.apiEvent.findMany({
      where: {
        source: "order_reconstruction",
        eventType: { in: ["skipped", "error"] },
        externalId: { not: null },
        createdAt: { gte: retryAfter }
      },
      select: { externalId: true }
    })
  ]);

  return [
    ...new Set(
      [
        ...orders.map((order) => order.externalId),
        ...recentSkips
          .map((event) => event.externalId)
          .filter((externalId): externalId is string => Boolean(externalId))
      ].filter(Boolean)
    )
  ];
}

async function updateShipmentCdekFieldsFromEntity(cdekNumber: string, entity: CdekOrderEntity, response: unknown) {
  const shipment = await prisma.shipment.findFirst({
    where: { trackNumber: cdekNumber },
    select: { id: true }
  });

  if (!shipment) {
    return;
  }

  await prisma.shipment.update({
    where: { id: shipment.id },
    data: {
      cdekUuid: entity.uuid,
      cdekNumber: entity.cdek_number ?? cdekNumber,
      cdekOrderNumber: entity.number,
      cdekDeliveryStatus: latestCdekStatusCode(entity) ?? undefined,
      cdekRecipientName: entity.recipient?.name ?? entity.recipient?.company,
      cdekRecipientPhone: normalizePhone(entity.recipient?.phones?.[0]?.number),
      cdekRecipientEmail: normalizeEmail(entity.recipient?.email),
      rawCdekOrderJson: stringifyJson(response)
    }
  });
}

async function findShipmentCandidates(contacts: { email?: string; phoneLast10?: string }): Promise<ShipmentWithProducts[]> {
  const orFilters = [];

  if (contacts.phoneLast10) {
    orFilters.push({ cdekRecipientPhone: { contains: contacts.phoneLast10 } });
    orFilters.push({ rawCdekOrderJson: { contains: contacts.phoneLast10 } });
  }

  if (contacts.email) {
    orFilters.push({ cdekRecipientEmail: contacts.email });
    orFilters.push({ rawCdekOrderJson: { contains: contacts.email } });
  }

  if (orFilters.length === 0) {
    return [];
  }

  return prisma.shipment.findMany({
    where: {
      orderId: null,
      products: { some: { isActive: true } },
      OR: orFilters
    },
    include: {
      products: {
        where: { isActive: true },
        include: { markCodes: { where: { isActive: true } } }
      }
    },
    orderBy: { updatedAt: "desc" },
    take: 200
  });
}

function uniqueShipments(shipments: ShipmentWithProducts[]): ShipmentWithProducts[] {
  const seen = new Set<number>();
  const result: ShipmentWithProducts[] = [];

  for (const shipment of shipments) {
    if (seen.has(shipment.id)) {
      continue;
    }

    seen.add(shipment.id);
    result.push(shipment);
  }

  return result;
}

function scoreShipmentCandidate(
  receipt: ReceiptWithItems,
  shipment: ShipmentWithProducts,
  contacts: { email?: string; phoneLast10?: string },
  productLines: ReceiptLine[]
): CandidateMatch | null {
  const productMatches = matchReceiptLinesToShipmentProducts(productLines, shipment.products);
  if (!productMatches) {
    return null;
  }

  const reasons: string[] = ["products"];
  let score = 100;

  if (contacts.phoneLast10 && shipment.cdekRecipientPhone?.includes(contacts.phoneLast10)) {
    score += 50;
    reasons.push("phone");
  }

  if (contacts.email && shipment.cdekRecipientEmail?.toLowerCase() === contacts.email) {
    score += 50;
    reasons.push("email");
  }

  if (shipment.cdekOrderNumber && receipt.invoiceId && normalizeLoose(shipment.cdekOrderNumber) === normalizeLoose(receipt.invoiceId)) {
    score += 100;
    reasons.push("cdek_order_number");
  }

  const dateScore = shipmentDateScore(receipt.dateTime, shipment.dateCreated);
  score += dateScore;
  if (dateScore > 0) {
    reasons.push("date");
  }

  return {
    shipment,
    productMatches,
    score,
    reasons
  };
}

function matchReceiptLinesToShipmentProducts(
  productLines: ReceiptLine[],
  products: ShipmentWithProducts["products"]
): ProductMatch[] | null {
  const unusedProducts = [...products];
  const matches: ProductMatch[] = [];

  for (const line of productLines) {
    const candidates = unusedProducts.filter((product) => {
      const quantity = product.factQty || product.planQty;
      return quantity === line.quantity && namesMatch(line.normalizedLabel, normalizeProductName(product.name));
    });

    if (candidates.length !== 1) {
      return null;
    }

    const product = candidates[0];
    matches.push({ line, product });
    unusedProducts.splice(unusedProducts.indexOf(product), 1);
  }

  const expectedProductQuantity = productLines.reduce((sum, line) => sum + line.quantity, 0);
  const shipmentProductQuantity = products.reduce((sum, product) => sum + (product.factQty || product.planQty), 0);

  return expectedProductQuantity === shipmentProductQuantity ? matches : null;
}

function buildOrderInput(
  receipt: ReceiptWithItems,
  match: CandidateMatch,
  contacts: { email?: string; phone?: string },
  productLines: ReceiptLine[],
  deliveryLines: ReceiptLine[]
): CreateOrderInput {
  const productItems = match.productMatches.map(({ line, product }) => ({
    ffProductId: product.ffProductId ?? undefined,
    clientCode: product.clientCode,
    name: product.name,
    quantity: line.quantity,
    unitPriceRub: kopToRub(line.priceKop),
    vat: line.vat,
    isMarked: true,
    objectCodeBeforeMark: 32,
    objectCodeWithMark: 33
  }));

  const deliveryItems = deliveryLines.map((line, index) => ({
    clientCode: `__delivery__:${line.itemId || index}`,
    name: line.label,
    quantity: line.quantity,
    unitPriceRub: kopToRub(line.priceKop),
    vat: line.vat,
    isMarked: false,
    objectCodeBeforeMark: line.objectCode,
    objectCodeWithMark: line.objectCode
  }));

  return {
    externalId: receipt.invoiceId ?? receipt.cloudKassirId,
    accountId: receipt.accountId ?? undefined,
    customerEmail: contacts.email,
    customerPhone: contacts.phone,
    paymentStatus: "paid",
    skladDispatchId: match.shipment.skladDispatchId,
    deliveryTrackNumber: match.shipment.trackNumber ?? undefined,
    items: [...productItems, ...deliveryItems]
  };
}

async function linkImportedFirstReceipt(orderId: number, receipt: ReceiptWithItems) {
  const status = (receipt.status || "processed").toLowerCase();

  await prisma.fiscalReceipt.upsert({
    where: { requestId: `imported-first:${receipt.cloudKassirId}` },
    create: {
      orderId,
      kind: "first_prepayment",
      cloudKassirId: receipt.cloudKassirId,
      type: receipt.type ?? "Income",
      status: ["processed", "queued", "sent"].includes(status) ? status : "processed",
      requestId: `imported-first:${receipt.cloudKassirId}`,
      invoiceId: receipt.invoiceId,
      payloadJson: receipt.rawJson
    },
    update: {
      orderId,
      cloudKassirId: receipt.cloudKassirId,
      type: receipt.type ?? "Income",
      status: ["processed", "queued", "sent"].includes(status) ? status : "processed",
      invoiceId: receipt.invoiceId,
      payloadJson: receipt.rawJson
    }
  });
}

function extractReceiptContacts(receipt: ReceiptWithItems) {
  const raw = parseRawJson(receipt.rawJson);
  const customerReceipt = isRecord(raw.CustomerReceipt) ? raw.CustomerReceipt : {};

  const email = normalizeEmail(
    readString(raw, ["Email", "email"]) ??
      readString(customerReceipt, ["Email", "email"]) ??
      receipt.accountId ??
      undefined
  );
  const phone = normalizePhone(readString(raw, ["Phone", "phone"]) ?? readString(customerReceipt, ["Phone", "phone"]));

  return {
    email,
    phone,
    phoneLast10: phone ? phone.slice(-10) : undefined
  };
}

function extractProductLines(receipt: ReceiptWithItems): ReceiptLine[] {
  return receipt.items
    .map(toReceiptLine)
    .filter((line): line is ReceiptLine => isReceiptLine(line) && !line.isDelivery);
}

function extractDeliveryLines(receipt: ReceiptWithItems): ReceiptLine[] {
  return receipt.items
    .map(toReceiptLine)
    .filter((line): line is ReceiptLine => isReceiptLine(line) && line.isDelivery);
}

function isReceiptLine(line: ReceiptLine | null): line is ReceiptLine {
  return line !== null;
}

function toReceiptLine(item: CloudKassirReceiptItem): ReceiptLine | null {
  const label = item.label?.trim();
  const quantity = parseQuantity(item.quantity);
  const priceKop = item.priceKop;
  const amountKop = item.amountKop;

  if (!label || !quantity || priceKop === null || amountKop === null) {
    return null;
  }

  const objectCode = item.object ?? 1;
  const normalizedLabel = normalizeProductName(label);

  return {
    itemId: item.id,
    label,
    normalizedLabel,
    quantity,
    priceKop,
    amountKop,
    vat: item.vat,
    objectCode,
    isDelivery: objectCode === 4 || /cdek|сдэк|сдек|доставк/i.test(label)
  };
}

function parseQuantity(quantity: string | null): number | null {
  if (!quantity) {
    return null;
  }

  const value = Number(quantity.replace(",", "."));
  return Number.isInteger(value) && value > 0 ? value : null;
}

function shipmentDateScore(receiptDate: string | null, shipmentDate: string | null): number {
  const receiptTime = parseCloudKassirDate(receiptDate);
  const shipmentTime = parseRussianDate(shipmentDate);

  if (!receiptTime || !shipmentTime) {
    return 0;
  }

  const diffDays = (shipmentTime.getTime() - receiptTime.getTime()) / (24 * 60 * 60 * 1000);
  if (diffDays >= -1 && diffDays <= 21) {
    return 20;
  }

  return 0;
}

function matchSummary(match: CandidateMatch) {
  return {
    skladDispatchId: match.shipment.skladDispatchId,
    trackNumber: match.shipment.trackNumber,
    score: match.score,
    reasons: match.reasons
  };
}

function namesMatch(left: string, right: string): boolean {
  return left === right || left.includes(right) || right.includes(left);
}

function normalizeProductName(value: string): string {
  return normalizeLoose(value)
    .replace(/\b(exo|system|box)\b/g, " $1 ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeLoose(value: string): string {
  return value
    .toLowerCase()
    .replace(/ё/g, "е")
    .replace(/[^a-zа-я0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeEmail(email: string | null | undefined): string | undefined {
  const normalized = email?.trim().toLowerCase();
  return normalized && normalized.includes("@") ? normalized : undefined;
}

function normalizePhone(phone: string | null | undefined): string | undefined {
  const digits = phone?.replace(/\D/g, "");
  return digits && digits.length >= 10 ? digits : undefined;
}

function extractCdekImNumber(invoiceId: string | null): string | undefined {
  const value = invoiceId?.trim();
  if (!value) {
    return undefined;
  }

  const candidate = value.includes(":") ? value.split(":").pop() : value;
  const normalized = candidate?.replace(/\D/g, "");
  return normalized && normalized.length > 0 ? normalized : undefined;
}

function latestCdekStatusCode(entity: CdekOrderEntity): string | undefined {
  const statuses = entity.statuses ?? [];
  const latestStatus = statuses[statuses.length - 1];
  return latestStatus?.code ?? undefined;
}

function normalizeReceiptDateBound(value: string | null | undefined): string | undefined {
  const trimmed = value?.trim();
  if (!trimmed) {
    return undefined;
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
    return trimmed;
  }

  const match = trimmed.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)/);
  if (!match) {
    return trimmed;
  }

  return `${match[1]} ${match[2].length === 5 ? `${match[2]}:00` : match[2]}`;
}

function parseRawJson(rawJson: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(rawJson);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function readString(payload: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value;
    }
  }

  return undefined;
}

function parseCloudKassirDate(value: string | null): Date | null {
  if (!value) {
    return null;
  }

  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!match) {
    return null;
  }

  const [, year, month, day, hour, minute, second = "00"] = match;
  return new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}+03:00`);
}

function parseRussianDate(value: string | null): Date | null {
  if (!value) {
    return null;
  }

  const match = value.match(/^(\d{2})\.(\d{2})\.(\d{4})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!match) {
    return null;
  }

  const [, day, month, year, hour, minute, second = "00"] = match;
  return new Date(`${year}-${month}-${day}T${hour}:${minute}:${second}+03:00`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
