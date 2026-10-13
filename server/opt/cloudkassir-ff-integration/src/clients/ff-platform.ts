import { config } from "../config.js";
import { fetchWithTimeout } from "../utils/fetch.js";

export type SkladDispatch = {
  id: number;
  shopId: number;
  skladId: number;
  status: string;
  date_created: string;
  date_at_work?: string | null;
  date_at_otgrugen?: string | null;
  delivery_performer_track_number?: string | null;
  billing_costs?: unknown;
};

export type CdekOtgruzka = {
  id: number;
  status: string;
  type_contract: string;
  number_contract?: string | null;
  dateCreated: string;
  dateClosed?: string | null;
};

export type CdekProduct = {
  id: number;
  client_code: string;
  name: string;
  plan_qty: number;
  fact_qty: number;
  kiz_codes?: Array<string | null> | null;
};

export type CdekOtgruzkaDetail = {
  otgruzka: CdekOtgruzka;
  products: CdekProduct[];
};

export class FfPlatformHttpError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
    readonly bodyText: string
  ) {
    super(`FF Platform ${path} failed with ${status}: ${bodyText.slice(0, 500)}`);
    this.name = "FfPlatformHttpError";
  }
}

export class FfPlatformClient {
  constructor(
    private readonly baseUrl = config.FF_PLATFORM_BASE_URL,
    private readonly token = config.FF_PLATFORM_TOKEN
  ) {}

  async getSkladDispatch(skladDispatchId: number): Promise<SkladDispatch> {
    return this.get(`/client_api/v1/sklad_dispatch/${skladDispatchId}`);
  }

  async getCdekDispatchDetail(skladDispatchId: number): Promise<CdekOtgruzkaDetail | null> {
    return this.get(`/client_api/v1/cdek_otgruzki/detail?id=${encodeURIComponent(skladDispatchId)}`);
  }

  async listCdekDispatches(params: {
    from: string;
    to?: string;
    status?: string;
  }): Promise<CdekOtgruzka[]> {
    const searchParams = new URLSearchParams({ from: params.from });
    if (params.to) {
      searchParams.set("to", params.to);
    }
    if (params.status) {
      searchParams.set("status", params.status);
    }

    return this.get(`/client_api/v1/cdek_otgruzki?${searchParams.toString()}`);
  }

  private async get<T>(path: string): Promise<T> {
    const response = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        headers: {
          token: this.token
        }
      },
      config.EXTERNAL_API_TIMEOUT_MS
    );

    const bodyText = await response.text();

    if (!response.ok) {
      throw new FfPlatformHttpError(path, response.status, bodyText);
    }

    const body = parseJsonBody(bodyText, path);
    return body as T;
  }
}

function parseJsonBody(bodyText: string, path: string): unknown {
  if (!bodyText) {
    return {};
  }

  try {
    return JSON.parse(bodyText);
  } catch (error) {
    throw new Error(`FF Platform ${path} returned non-JSON response: ${bodyText.slice(0, 500)}`);
  }
}
