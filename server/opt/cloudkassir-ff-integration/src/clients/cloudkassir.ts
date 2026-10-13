import { config } from "../config.js";
import { fetchWithTimeout } from "../utils/fetch.js";

export type CloudKassirReceiptType = "Income" | "IncomeReturn" | "Expense" | "ExpenseReturn";

export type CloudKassirReceiptItem = {
  Label: string;
  Price: number;
  Quantity: string | number;
  Amount: number;
  Vat?: number | null;
  Method?: number;
  Object?: number;
  UnitCode?: number;
  RawMarkCode?: string;
  GroupSeparator?: string;
  ExpectedMarkItemStatus?: number;
};

export type CloudKassirCreateReceiptRequest = {
  Inn: string;
  Type: CloudKassirReceiptType;
  CustomerReceipt: {
    Items: CloudKassirReceiptItem[];
    TaxationSystem: number;
    Amounts: {
      Electronic?: number;
      AdvancePayment?: number;
      Credit?: number;
      Provision?: number;
    };
    CalculationPlace?: string;
    Email?: string;
    Phone?: string;
    IsInternetPayment?: boolean;
    RussiaTimeZone?: number;
  };
  InvoiceId?: string;
  AccountId?: string;
};

export type CloudKassirApiResponse<TModel = unknown> = {
  Success: boolean;
  Message?: string | null;
  Model?: TModel;
  InnerResult?: unknown;
  Warnings?: Array<{ Code: number; Description: string }>;
};

export type CloudKassirReceiptListItem = Record<string, unknown>;

export type CloudKassirReceiptListRequest = {
  FiscalDateFrom: string;
  FiscalDateTill: string;
  PageNumber: number;
  Limit: number;
};

export type CloudPaymentsTransaction = {
  TransactionId: number;
  Amount?: number;
  Currency?: string;
  InvoiceId?: string | null;
  AccountId?: string | null;
  Email?: string | null;
  CreatedDateIso?: string | null;
  AuthDateIso?: string | null;
  ConfirmDateIso?: string | null;
  CardFirstSix?: string | null;
  CardLastFour?: string | null;
  CardType?: string | null;
  Status?: string | null;
  StatusCode?: number | null;
  Reason?: string | null;
  ReasonCode?: number | null;
  Type?: number | null;
};

export type CloudPaymentsTransactionListRequest = {
  CreatedDateGte: string;
  CreatedDateLte: string;
  PageNumber: number;
  TimeZone?: string;
  Statuses?: Array<"Authorized" | "Completed" | "Cancelled" | "Declined">;
};

export type CloudKassirReceiptStatus = "Queued" | "Processed" | "Error" | "NotFound" | string;

export type MarkCodeValidationResponse = CloudKassirApiResponse<never> & {
  Results?: Array<{
    MarkCode: string;
    IsValid: boolean;
    Message: string | null;
    IndustryRequisite: unknown | null;
  }>;
};

export class CloudKassirClient {
  constructor(
    private readonly baseUrl = config.CLOUDKASSIR_BASE_URL,
    private readonly publicId = config.CLOUDKASSIR_PUBLIC_ID,
    private readonly apiSecret = config.CLOUDKASSIR_API_SECRET
  ) {}

  async createReceipt(
    payload: CloudKassirCreateReceiptRequest,
    requestId: string
  ): Promise<CloudKassirApiResponse<string | { Id?: string }>> {
    return this.post("/kkt/receipt", payload, requestId);
  }

  async getReceiptStatus(id: string): Promise<CloudKassirApiResponse<CloudKassirReceiptStatus>> {
    return this.post("/kkt/receipt/status/get", { Id: id });
  }

  async getReceipt(id: string): Promise<CloudKassirApiResponse> {
    return this.post("/kkt/receipt/get", { Id: id });
  }

  async listReceiptsByFiscalPeriod(
    payload: CloudKassirReceiptListRequest
  ): Promise<CloudKassirReceiptListItem[]> {
    const response = await this.post<unknown>("/kkt/receipts", payload);

    if (!Array.isArray(response)) {
      const message =
        response && typeof response === "object" && "Message" in response
          ? String((response as { Message?: unknown }).Message)
          : "unexpected response";
      throw new Error(`CloudKassir /kkt/receipts returned ${message}`);
    }

    return response.filter((item): item is CloudKassirReceiptListItem => {
      return item !== null && typeof item === "object" && !Array.isArray(item);
    });
  }

  async listPaymentsByPeriod(
    payload: CloudPaymentsTransactionListRequest
  ): Promise<CloudKassirApiResponse<CloudPaymentsTransaction[]>> {
    return this.post("/v2/payments/list", payload);
  }

  async getPayment(transactionId: string | number): Promise<CloudKassirApiResponse<CloudPaymentsTransaction>> {
    return this.post("/payments/get", { TransactionId: transactionId });
  }

  async getMerchantDeviceWarnings(): Promise<CloudKassirApiResponse> {
    return this.get("/kkt/merchant-devices-warnings");
  }

  async getReceiptNotificationSettings(): Promise<CloudKassirApiResponse> {
    return this.post("/site/notifications/receipt/get", {});
  }

  async validateMarkCodes(markCodes: string[]): Promise<MarkCodeValidationResponse> {
    return this.post("/kkt/validate-mark-codes", {
      Inn: config.CLOUDKASSIR_INN,
      MarkCodes: markCodes
    });
  }

  private async post<T>(path: string, payload: unknown, requestId?: string): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Authorization: this.basicAuthHeader()
    };

    if (requestId) {
      headers["X-Request-ID"] = requestId;
    }

    const response = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        method: "POST",
        headers,
        body: JSON.stringify(payload)
      },
      config.EXTERNAL_API_TIMEOUT_MS
    );

    const bodyText = await response.text();

    if (!response.ok) {
      throw new Error(`CloudKassir ${path} failed with ${response.status}: ${bodyText}`);
    }

    const body = parseJsonBody(bodyText, path);
    return body as T;
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        method: "GET",
        headers: {
          Authorization: this.basicAuthHeader()
        }
      },
      config.EXTERNAL_API_TIMEOUT_MS
    );

    const bodyText = await response.text();

    if (!response.ok) {
      throw new Error(`CloudKassir ${path} failed with ${response.status}: ${bodyText}`);
    }

    const body = parseJsonBody(bodyText, path);
    return body as T;
  }

  private basicAuthHeader(): string {
    const token = Buffer.from(`${this.publicId}:${this.apiSecret}`).toString("base64");
    return `Basic ${token}`;
  }
}

function parseJsonBody(bodyText: string, path: string): unknown {
  if (!bodyText) {
    return {};
  }

  try {
    return JSON.parse(bodyText);
  } catch (error) {
    throw new Error(`CloudKassir ${path} returned non-JSON response: ${bodyText.slice(0, 500)}`);
  }
}
