import type { CloudKassirApiResponse, CloudPaymentsTransaction } from "../clients/cloudkassir.js";
import { CloudKassirClient } from "../clients/cloudkassir.js";
import { prisma } from "../db.js";
import { stringifyJson } from "../utils/json.js";
import { rubToKop } from "../utils/money.js";

type ImportTransactionsInput = {
  from: string;
  to: string;
  timeZone?: string;
  statuses?: Array<"Authorized" | "Completed" | "Cancelled" | "Declined">;
  maxPages?: number;
};

type ImportReceiptsByFiscalPeriodInput = {
  from: string;
  to: string;
  limit?: number;
  maxPages?: number;
};

type ReceiptPayload = Record<string, unknown>;

export class CloudKassirImportService {
  constructor(private readonly cloudKassirClient = new CloudKassirClient()) {}

  async importTransactionsByPeriod(input: ImportTransactionsInput) {
    const maxPages = input.maxPages ?? 100;
    let imported = 0;
    const apiResponses: Array<{
      from: string;
      to: string;
      pageNumber: number;
      count: number;
      success: boolean;
      message?: string | null;
    }> = [];

    for (const chunk of splitPeriodIntoDailyChunks(input.from, input.to)) {
      let pageNumber = 1;

      while (pageNumber <= maxPages) {
        const response = await this.cloudKassirClient.listPaymentsByPeriod({
          CreatedDateGte: chunk.from,
          CreatedDateLte: chunk.to,
          PageNumber: pageNumber,
          TimeZone: input.timeZone,
          Statuses: input.statuses
        });

        const transactions = response.Model ?? [];
        apiResponses.push({
          from: chunk.from,
          to: chunk.to,
          pageNumber,
          count: transactions.length,
          success: response.Success,
          message: response.Message
        });

        if (!response.Success) {
          throw new Error(`CloudPayments transactions import failed: ${response.Message ?? "unknown error"}`);
        }

        for (const transaction of transactions) {
          await this.upsertTransaction(transaction);
        }

        imported += transactions.length;

        if (transactions.length < 100) {
          break;
        }

        pageNumber += 1;
      }
    }

    const result = {
      imported,
      pages: apiResponses
    };

    await prisma.apiEvent.create({
      data: {
        source: "cloudkassir",
        eventType: "transactions_import_result",
        externalId: `${input.from}..${input.to}`,
        payloadJson: stringifyJson(result)
      }
    });

    return result;
  }

  async importReceiptById(id: string) {
    const response = await this.cloudKassirClient.getReceipt(id);

    if (!response.Success) {
      if (isReceiptNotFoundResponse(response)) {
        await this.markReceiptNotFound(id, response);
      }
      await this.saveReceiptImportError(id, response);
      return {
        id,
        imported: false,
        response
      };
    }

    const payload = unwrapReceiptPayload(id, response);
    const receipt = await this.upsertReceipt(payload, "api_get");

    return {
      id,
      imported: true,
      receipt,
      response
    };
  }

  async importReceiptsByIds(ids: string[]) {
    const results = [];

    for (const id of ids) {
      results.push(await this.importReceiptById(id));
    }

    return {
      requested: ids.length,
      imported: results.filter((result) => result.imported).length,
      results
    };
  }

  async importReceiptsByFiscalPeriod(input: ImportReceiptsByFiscalPeriodInput) {
    const limit = input.limit ?? 100;
    const maxPages = input.maxPages ?? 100;
    let imported = 0;
    const pages: Array<{
      from: string;
      to: string;
      pageNumber: number;
      count: number;
    }> = [];

    for (const chunk of splitPeriodIntoDailyChunks(input.from, input.to)) {
      let pageNumber = 0;

      while (pageNumber < maxPages) {
        const receipts = await this.cloudKassirClient.listReceiptsByFiscalPeriod({
          FiscalDateFrom: chunk.from,
          FiscalDateTill: chunk.to,
          PageNumber: pageNumber,
          Limit: limit
        });

        pages.push({
          from: chunk.from,
          to: chunk.to,
          pageNumber,
          count: receipts.length
        });

        for (const receipt of receipts) {
          await this.upsertReceipt(receipt, "api_list");
        }

        imported += receipts.length;

        if (receipts.length < limit) {
          break;
        }

        pageNumber += 1;
      }
    }

    const result = {
      imported,
      pages
    };

    await prisma.apiEvent.create({
      data: {
        source: "cloudkassir",
        eventType: "receipt_list_import_result",
        externalId: `${input.from}..${input.to}`,
        payloadJson: stringifyJson(result)
      }
    });

    return result;
  }

  async importReceiptWebhook(payload: ReceiptPayload) {
    return this.upsertReceipt(payload, "webhook");
  }

  async fetchCashRegisterSnapshot() {
    const [warnings, receiptNotificationSettings] = await Promise.all([
      this.cloudKassirClient.getMerchantDeviceWarnings(),
      this.cloudKassirClient.getReceiptNotificationSettings()
    ]);

    const snapshot = {
      warnings,
      receiptNotificationSettings
    };

    await prisma.apiEvent.create({
      data: {
        source: "cloudkassir",
        eventType: "cash_register_snapshot",
        payloadJson: stringifyJson(snapshot)
      }
    });

    return snapshot;
  }

  private async upsertTransaction(transaction: CloudPaymentsTransaction) {
    return prisma.cloudPaymentTransaction.upsert({
      where: { transactionId: String(transaction.TransactionId) },
      create: {
        transactionId: String(transaction.TransactionId),
        invoiceId: transaction.InvoiceId,
        accountId: transaction.AccountId,
        email: transaction.Email,
        amountKop: transactionAmountKop(transaction.Amount),
        currency: transaction.Currency,
        operationType: transaction.Type === undefined ? undefined : transaction.Type === null ? null : String(transaction.Type),
        status: transaction.Status,
        statusCode: transaction.StatusCode,
        reason: transaction.Reason,
        reasonCode: transaction.ReasonCode,
        cardFirstSix: transaction.CardFirstSix,
        cardLastFour: transaction.CardLastFour,
        cardType: transaction.CardType,
        paymentDate: transactionPaymentDate(transaction),
        rawJson: stringifyJson(transaction)
      },
      update: {
        invoiceId: transaction.InvoiceId,
        accountId: transaction.AccountId,
        email: transaction.Email,
        amountKop: transactionAmountKop(transaction.Amount),
        currency: transaction.Currency,
        operationType: transaction.Type === undefined ? undefined : transaction.Type === null ? null : String(transaction.Type),
        status: transaction.Status,
        statusCode: transaction.StatusCode,
        reason: transaction.Reason,
        reasonCode: transaction.ReasonCode,
        cardFirstSix: transaction.CardFirstSix,
        cardLastFour: transaction.CardLastFour,
        cardType: transaction.CardType,
        paymentDate: transactionPaymentDate(transaction),
        rawJson: stringifyJson(transaction)
      }
    });
  }

  private async upsertReceipt(payload: ReceiptPayload, source: string) {
    const expandedPayload = expandReceiptPayload(payload);
    const id = readString(expandedPayload, ["Id", "id"]);
    if (!id) {
      throw new Error("CloudKassir receipt payload does not contain Id");
    }

    const receiptData = normalizeReceipt(expandedPayload, source);
    const items = extractReceiptItems(expandedPayload);

    return prisma.$transaction(async (tx) => {
      const receipt = await tx.cloudKassirReceipt.upsert({
        where: { cloudKassirId: id },
        create: receiptData,
        update: receiptData
      });

      await tx.cloudKassirReceiptItem.deleteMany({
        where: { receiptId: receipt.id }
      });

      for (const item of items) {
        await tx.cloudKassirReceiptItem.create({
          data: {
            receiptId: receipt.id,
            label: readString(item, ["Label", "label", "Name", "name"]),
            priceKop: readNumberAsKop(item, ["Price", "price"]),
            quantity: readStringOrNumber(item, ["Quantity", "quantity"]),
            amountKop: readNumberAsKop(item, ["Amount", "amount"]),
            vat: readNumber(item, ["Vat", "vat"]),
            method: readNumber(item, ["Method", "method"]),
            object: readNumber(item, ["Object", "object"]),
            rawMarkCode: readString(item, ["RawMarkCode", "rawMarkCode"]),
            rawJson: stringifyJson(item)
          }
        });
      }

      const fiscalStatus = normalizeFiscalReceiptStatus(receiptData.status);
      if (fiscalStatus) {
        await tx.fiscalReceipt.updateMany({
          where: { cloudKassirId: id },
          data: {
            type: receiptData.type,
            status: fiscalStatus,
            responseJson: receiptData.rawJson,
            errorMessage: fiscalStatus === "error" ? readString(expandedPayload, ["Message", "Error", "StatusDescription"]) ?? "CloudKassir receipt error" : null
          }
        });
      }

      return tx.cloudKassirReceipt.findUniqueOrThrow({
        where: { id: receipt.id },
        include: { items: true }
      });
    });
  }

  private async markReceiptNotFound(id: string, response: CloudKassirApiResponse) {
    await prisma.fiscalReceipt.updateMany({
      where: {
        cloudKassirId: id,
        status: { in: ["sent", "queued"] }
      },
      data: {
        status: "not_found",
        errorMessage: response.Message ?? "CloudKassir receipt not found",
        responseJson: stringifyJson(response)
      }
    });
  }

  private async saveReceiptImportError(id: string, response: CloudKassirApiResponse) {
    await prisma.apiEvent.create({
      data: {
        source: "cloudkassir",
        eventType: "receipt_import_error",
        externalId: id,
        payloadJson: stringifyJson(response)
      }
    });
  }
}

function isReceiptNotFoundResponse(response: CloudKassirApiResponse): boolean {
  return !response.Success && /receipt\s+not\s+found/i.test(String(response.Message ?? ""));
}

function unwrapReceiptPayload(id: string, response: CloudKassirApiResponse): ReceiptPayload {
  if (response.Model && typeof response.Model === "object") {
    const model = response.Model as ReceiptPayload;
    const additionalData = model.AdditionalData;

    if (isRecord(additionalData)) {
      return expandReceiptPayload({
        ...model,
        Id: readString(additionalData, ["Id"]) ?? id
      });
    }

    return {
      ...model,
      Id: readString(model, ["Id"]) ?? id
    };
  }

  return {
    Id: id,
    RawResponse: response
  };
}

function expandReceiptPayload(payload: ReceiptPayload): ReceiptPayload {
  const additionalData = payload.AdditionalData;

  if (!isRecord(additionalData)) {
    return payload;
  }

  return {
    ...additionalData,
    Status: readString(payload, ["Status", "status"]) ?? readString(additionalData, ["Status", "status"]),
    Amount: readNumber(additionalData, ["Amount"]) ?? readNestedAmount(payload),
    Receipt: {
      Items: Array.isArray(payload.Items) ? payload.Items : []
    },
    CustomerReceipt: payload,
    Id: readString(additionalData, ["Id"]) ?? readString(payload, ["Id", "id"])
  };
}

function normalizeReceipt(payload: ReceiptPayload, source: string) {
  const id = readString(payload, ["Id", "id"]);

  if (!id) {
    throw new Error("CloudKassir receipt payload does not contain Id");
  }

  return {
    cloudKassirId: id,
    invoiceId: readString(payload, ["InvoiceId", "invoiceId"]),
    accountId: readString(payload, ["AccountId", "accountId"]),
    transactionId: readStringOrNumber(payload, ["TransactionId", "transactionId"]),
    type: readString(payload, ["Type", "type"]),
    status: readString(payload, ["Status", "status"]) ?? inferReceiptStatus(payload),
    amountKop: readNumberAsKop(payload, ["Amount", "amount"]),
    dateTime: readString(payload, ["DateTime", "dateTime"]),
    documentNumber: readStringOrNumber(payload, ["DocumentNumber", "documentNumber"]),
    fiscalSign: readString(payload, ["FiscalSign", "fiscalSign"]),
    deviceNumber: readStringOrNumber(payload, ["DeviceNumber", "deviceNumber"]),
    regNumber: readString(payload, ["RegNumber", "regNumber"]),
    fiscalNumber: readString(payload, ["FiscalNumber", "fiscalNumber"]),
    inn: readStringOrNumber(payload, ["Inn", "OrganizationInn", "inn"]),
    ofd: readString(payload, ["Ofd", "ofd"]),
    url: readString(payload, ["Url", "ReceiptOfdUrl", "ReceiptLocalUrl", "OfdReceiptUrl", "url"]),
    qrCodeUrl: readString(payload, ["QrCodeUrl", "QrCodeUrl", "qrCodeUrl"]),
    source,
    rawJson: stringifyJson(payload)
  };
}

function inferReceiptStatus(payload: ReceiptPayload): string {
  if (hasFiscalAttributes(payload)) {
    return "processed";
  }

  return "imported";
}

function hasFiscalAttributes(payload: ReceiptPayload): boolean {
  return Boolean(
    readStringOrNumber(payload, ["DocumentNumber", "documentNumber"]) &&
      readString(payload, ["FiscalSign", "fiscalSign"]) &&
      readStringOrNumber(payload, ["FiscalNumber", "fiscalNumber"])
  );
}

function normalizeFiscalReceiptStatus(status: string | undefined): string | undefined {
  switch (status?.toLowerCase()) {
    case "processed":
      return "processed";
    case "queued":
      return "queued";
    case "error":
      return "error";
    case "notfound":
    case "not_found":
      return "not_found";
    default:
      return undefined;
  }
}

function extractReceiptItems(payload: ReceiptPayload): ReceiptPayload[] {
  const directReceipt = payload.Receipt;
  if (directReceipt && typeof directReceipt === "object") {
    const items = (directReceipt as ReceiptPayload).Items ?? (directReceipt as ReceiptPayload).items;
    if (Array.isArray(items)) {
      return items.filter(isRecord);
    }
  }

  const customerReceipt = payload.CustomerReceipt;
  if (customerReceipt && typeof customerReceipt === "object") {
    const items = (customerReceipt as ReceiptPayload).Items ?? (customerReceipt as ReceiptPayload).items;
    if (Array.isArray(items)) {
      return items.filter(isRecord);
    }
  }

  const items = payload.Items ?? payload.items;
  if (Array.isArray(items)) {
    return items.filter(isRecord);
  }

  return [];
}

function transactionAmountKop(amount: number | null | undefined): number | null | undefined {
  if (amount === undefined || amount === null) {
    return amount;
  }

  return rubToKop(amount);
}

function transactionPaymentDate(transaction: CloudPaymentsTransaction): string | null | undefined {
  let sawNull = false;
  for (const value of [transaction.ConfirmDateIso, transaction.AuthDateIso, transaction.CreatedDateIso]) {
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
    if (value === null) {
      sawNull = true;
    }
  }

  return sawNull ? null : undefined;
}

function readString(payload: ReceiptPayload, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
  }

  return undefined;
}

function readStringOrNumber(payload: ReceiptPayload, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
    if (typeof value === "number") {
      return String(value);
    }
  }

  return undefined;
}

function readNumber(payload: ReceiptPayload, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "number") {
      return value;
    }
    if (typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))) {
      return Number(value);
    }
  }

  return undefined;
}

function readNumberAsKop(payload: ReceiptPayload, keys: string[]): number | undefined {
  const value = readNumber(payload, keys);
  return value === undefined ? undefined : rubToKop(value);
}

function readNestedAmount(payload: ReceiptPayload): number | undefined {
  const amounts = payload.Amounts;

  if (isRecord(amounts)) {
    return readNumber(amounts, ["Sum", "sum", "Electronic", "electronic"]);
  }

  return undefined;
}

function isRecord(value: unknown): value is ReceiptPayload {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function splitPeriodIntoDailyChunks(from: string, to: string) {
  const fromDate = new Date(from);
  const toDate = new Date(to);

  if (Number.isNaN(fromDate.getTime()) || Number.isNaN(toDate.getTime())) {
    throw new Error("Invalid period dates for CloudPayments import");
  }

  if (fromDate >= toDate) {
    throw new Error("CloudPayments import period `from` must be before `to`");
  }

  const chunks: Array<{ from: string; to: string }> = [];
  let cursor = fromDate;

  while (cursor < toDate) {
    const next = new Date(Math.min(cursor.getTime() + 24 * 60 * 60 * 1000, toDate.getTime()));

    chunks.push({
      from: cursor.toISOString(),
      to: next.toISOString()
    });

    cursor = next;
  }

  return chunks;
}
