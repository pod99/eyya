import type { Order, OrderItem, MarkCode } from "@prisma/client";
import { config } from "../config.js";
import type { CloudKassirCreateReceiptRequest, CloudKassirReceiptItem } from "../clients/cloudkassir.js";
import { kopToRub, rubToKop } from "../utils/money.js";

const GROUP_SEPARATOR = "\u001D";

type OrderWithItems = Order & {
  items: OrderItem[];
};

type MarkCodeWithItem = MarkCode & {
  orderItem?: OrderItem | null;
};

type ImportedReturnReceipt = {
  cloudKassirId: string;
  amountKop: number | null;
  items: Array<{
    label: string | null;
    priceKop: number | null;
    quantity: string | null;
    amountKop: number | null;
  }>;
};

export function buildFirstPrepaymentReceipt(order: OrderWithItems): CloudKassirCreateReceiptRequest {
  const items = order.items.map((item): CloudKassirReceiptItem => {
    return {
      Label: item.name,
      Price: kopToRub(item.unitPriceKop),
      Quantity: item.quantity,
      Amount: kopToRub(item.amountKop),
      Vat: item.vat,
      Method: 1,
      Object: item.objectCodeBeforeMark,
      UnitCode: 0
    };
  });

  return baseReceipt(order, "Income", items, {
    Electronic: kopToRub(order.amountKop)
  });
}

export function buildSecondFullPayReceipt(
  order: OrderWithItems,
  markCodes: MarkCodeWithItem[]
): CloudKassirCreateReceiptRequest {
  const markedLines = markCodes.map((markCode): CloudKassirReceiptItem => {
    if (!markCode.orderItem) {
      throw new Error(`Mark code ${markCode.id} is not linked to an order item`);
    }
    if (!markCode.orderItem.isMarked) {
      throw new Error(`Mark code ${markCode.id} is linked to unmarked order item ${markCode.orderItemId}`);
    }

    return {
      Label: markCode.orderItem.name,
      Price: kopToRub(markCode.orderItem.unitPriceKop),
      Quantity: 1,
      Amount: kopToRub(markCode.orderItem.unitPriceKop),
      Vat: markCode.orderItem.vat,
      Method: 4,
      Object: markCode.orderItem.objectCodeWithMark,
      UnitCode: 0,
      RawMarkCode: normalizeRawMarkCode(markCode.code),
      GroupSeparator: GROUP_SEPARATOR,
      ExpectedMarkItemStatus: 1
    };
  });

  const markedByItemId = new Map<number, number>();
  for (const markCode of markCodes) {
    if (!markCode.orderItemId) {
      continue;
    }

    markedByItemId.set(markCode.orderItemId, (markedByItemId.get(markCode.orderItemId) ?? 0) + 1);
  }

  const unmarkedOrRemainderLines = order.items.flatMap((item): CloudKassirReceiptItem[] => {
    const markedQuantity = markedByItemId.get(item.id) ?? 0;
    const remainingQuantity = item.quantity - markedQuantity;

    if (remainingQuantity <= 0) {
      return [];
    }

    return [
      {
        Label: item.name,
        Price: kopToRub(item.unitPriceKop),
        Quantity: remainingQuantity,
        Amount: kopToRub(item.unitPriceKop * remainingQuantity),
        Vat: item.vat,
        Method: 4,
        Object: item.objectCodeBeforeMark,
        UnitCode: 0
      }
    ];
  });

  return baseReceipt(order, "Income", [...markedLines, ...unmarkedOrRemainderLines], {
    AdvancePayment: kopToRub(order.amountKop)
  });
}

export function buildSecondFullPayReturnReceipt(
  order: OrderWithItems,
  secondFullPayPayloadJson: string,
  returnReceipt: ImportedReturnReceipt,
  previousReturnPayloadJsons: string[] = []
): CloudKassirCreateReceiptRequest {
  const secondItems = extractPayloadItems(secondFullPayPayloadJson);
  const previouslyReturnedMarkCodes = new Set(previousReturnPayloadJsons.flatMap(extractPayloadRawMarkCodes));
  const returnAmountKop = receiptAmountKop(returnReceipt);
  const secondAmountKop = sumItemsKop(secondItems);
  const items =
    amountsEqual(returnAmountKop, secondAmountKop) && previouslyReturnedMarkCodes.size === 0
      ? secondItems
      : selectReturnedItems(secondItems, returnReceipt, previouslyReturnedMarkCodes);

  if (items.length === 0) {
    throw new Error(`Cannot build full-pay return receipt for empty return receipt ${returnReceipt.cloudKassirId}`);
  }

  const returnItems = items.map(asReturnItem);
  const amountKop = sumItemsKop(returnItems);

  if (!amountsEqual(amountKop, returnAmountKop)) {
    throw new Error(
      `Full-pay return amount mismatch for ${returnReceipt.cloudKassirId}: return receipt ${returnAmountKop} kop, payload ${amountKop} kop`
    );
  }

  return baseReceipt(order, "IncomeReturn", returnItems, {
    AdvancePayment: kopToRub(amountKop)
  });
}

function baseReceipt(
  order: Order,
  type: CloudKassirCreateReceiptRequest["Type"],
  items: CloudKassirReceiptItem[],
  amounts: CloudKassirCreateReceiptRequest["CustomerReceipt"]["Amounts"]
): CloudKassirCreateReceiptRequest {
  return {
    Inn: config.CLOUDKASSIR_INN,
    Type: type,
    InvoiceId: order.externalId,
    AccountId: order.accountId ?? undefined,
    CustomerReceipt: {
      Items: items,
      TaxationSystem: config.CLOUDKASSIR_TAXATION_SYSTEM,
      Amounts: amounts,
      CalculationPlace: config.CLOUDKASSIR_CALCULATION_PLACE || undefined,
      Email: order.customerEmail ?? undefined,
      Phone: order.customerPhone ?? undefined,
      IsInternetPayment: true,
      RussiaTimeZone: config.CLOUDKASSIR_RUSSIA_TIME_ZONE
    }
  };
}

function normalizeRawMarkCode(code: string): string {
  return code.replace(new RegExp(`^${GROUP_SEPARATOR}+`), "");
}

function extractPayloadItems(payloadJson: string): CloudKassirReceiptItem[] {
  const payload = parsePayload(payloadJson);
  const customerReceipt = readRecord(payload.CustomerReceipt) ?? readRecord(payload.customerReceipt);
  const items = customerReceipt?.Items ?? customerReceipt?.items;

  if (!Array.isArray(items)) {
    throw new Error("Second full-pay receipt payload does not contain CustomerReceipt.Items");
  }

  return items.map(normalizePayloadItem);
}

function extractPayloadRawMarkCodes(payloadJson: string): string[] {
  try {
    return extractPayloadItems(payloadJson)
      .map((item) => item.RawMarkCode)
      .filter((code): code is string => typeof code === "string" && code.length > 0)
      .map(normalizeRawMarkCode);
  } catch {
    return [];
  }
}

function parsePayload(payloadJson: string): Record<string, unknown> {
  const parsed = JSON.parse(payloadJson);
  if (!readRecord(parsed)) {
    throw new Error("Fiscal receipt payload JSON is not an object");
  }

  return parsed;
}

function normalizePayloadItem(item: unknown): CloudKassirReceiptItem {
  const record = readRecord(item);
  if (!record) {
    throw new Error("Fiscal receipt item is not an object");
  }

  return {
    Label: readString(record, "Label") ?? readString(record, "label") ?? "",
    Price: readNumber(record, "Price") ?? readNumber(record, "price") ?? 0,
    Quantity: readNumber(record, "Quantity") ?? readNumber(record, "quantity") ?? 1,
    Amount: readNumber(record, "Amount") ?? readNumber(record, "amount") ?? 0,
    Vat: readNullableNumber(record, "Vat") ?? readNullableNumber(record, "vat"),
    Method: readNumber(record, "Method") ?? readNumber(record, "method"),
    Object: readNumber(record, "Object") ?? readNumber(record, "object"),
    UnitCode: readNumber(record, "UnitCode") ?? readNumber(record, "unitCode"),
    RawMarkCode: readString(record, "RawMarkCode") ?? readString(record, "rawMarkCode"),
    GroupSeparator: readString(record, "GroupSeparator") ?? readString(record, "groupSeparator"),
    ExpectedMarkItemStatus: readNumber(record, "ExpectedMarkItemStatus") ?? readNumber(record, "expectedMarkItemStatus")
  };
}

function selectReturnedItems(
  secondItems: CloudKassirReceiptItem[],
  returnReceipt: ImportedReturnReceipt,
  previouslyReturnedMarkCodes: Set<string>
): CloudKassirReceiptItem[] {
  const selected: CloudKassirReceiptItem[] = [];
  const usedIndexes = new Set<number>();

  for (const returnItem of returnReceipt.items) {
    const label = normalizeLabel(returnItem.label ?? "");
    const amountKop = returnItem.amountKop;
    if (!label || amountKop === null || amountKop === undefined || amountKop <= 0) {
      continue;
    }

    const matchingIndexes = secondItems
      .map((item, index) => ({ item, index }))
      .filter(({ item, index }) => {
        if (usedIndexes.has(index)) {
          return false;
        }
        if (normalizeLabel(item.Label) !== label) {
          return false;
        }
        const rawMarkCode = item.RawMarkCode ? normalizeRawMarkCode(item.RawMarkCode) : null;
        return !rawMarkCode || !previouslyReturnedMarkCodes.has(rawMarkCode);
      });

    if (matchingIndexes.length === 0) {
      throw new Error(`Cannot match return item "${returnItem.label}" from receipt ${returnReceipt.cloudKassirId}`);
    }

    const markedIndexes = matchingIndexes.filter(({ item }) => Boolean(item.RawMarkCode));
    if (markedIndexes.length > 0) {
      const quantity = parseReturnQuantity(returnItem.quantity);
      if (!Number.isInteger(quantity) || quantity <= 0) {
        throw new Error(`Marked return item "${returnItem.label}" must have an integer quantity`);
      }

      const chosen = markedIndexes.slice(0, quantity);
      if (chosen.length !== quantity) {
        throw new Error(`Not enough marked lines for return item "${returnItem.label}"`);
      }

      const chosenAmountKop = sumItemsKop(chosen.map(({ item }) => item));
      if (!amountsEqual(chosenAmountKop, amountKop)) {
        throw new Error(
          `Marked return amount mismatch for "${returnItem.label}": return ${amountKop} kop, selected ${chosenAmountKop} kop`
        );
      }

      for (const { item, index } of chosen) {
        usedIndexes.add(index);
        selected.push(item);
      }
      continue;
    }

    const [{ item, index }] = matchingIndexes;
    usedIndexes.add(index);
    selected.push({
      Label: item.Label,
      Price: kopToRub(returnItem.priceKop ?? Math.round(amountKop / parseReturnQuantity(returnItem.quantity))),
      Quantity: parseReturnQuantity(returnItem.quantity),
      Amount: kopToRub(amountKop),
      Vat: item.Vat,
      Method: item.Method,
      Object: item.Object,
      UnitCode: item.UnitCode
    });
  }

  return selected;
}

function asReturnItem(item: CloudKassirReceiptItem): CloudKassirReceiptItem {
  const rawMarkCode = item.RawMarkCode ? normalizeRawMarkCode(item.RawMarkCode) : undefined;

  return {
    ...item,
    Method: 4,
    RawMarkCode: rawMarkCode,
    GroupSeparator: rawMarkCode ? item.GroupSeparator ?? GROUP_SEPARATOR : undefined,
    ExpectedMarkItemStatus: rawMarkCode ? 3 : undefined
  };
}

function receiptAmountKop(receipt: ImportedReturnReceipt): number {
  if (receipt.amountKop !== null && receipt.amountKop !== undefined) {
    return receipt.amountKop;
  }

  return receipt.items.reduce((sum, item) => sum + (item.amountKop ?? 0), 0);
}

function sumItemsKop(items: CloudKassirReceiptItem[]): number {
  return items.reduce((sum, item) => sum + rubToKop(Number(item.Amount)), 0);
}

function amountsEqual(leftKop: number, rightKop: number): boolean {
  return Math.abs(leftKop - rightKop) <= 1;
}

function parseReturnQuantity(quantity: string | null): number {
  if (!quantity) {
    return 1;
  }

  const parsed = Number(quantity);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
}

function normalizeLabel(label: string): string {
  return label.trim().replace(/\s+/g, " ").toLowerCase();
}

function readRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function readString(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function readNumber(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key];
  if (typeof value === "number") {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
    return Number(value);
  }

  return undefined;
}

function readNullableNumber(record: Record<string, unknown>, key: string): number | null | undefined {
  if (record[key] === null) {
    return null;
  }

  return readNumber(record, key);
}
