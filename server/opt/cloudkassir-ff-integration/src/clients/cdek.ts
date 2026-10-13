import { config } from "../config.js";
import { fetchWithTimeout } from "../utils/fetch.js";

export type CdekOrderRecipient = {
  company?: string | null;
  name?: string | null;
  email?: string | null;
  phones?: Array<{ number?: string | null }>;
};

export type CdekOrderEntity = {
  uuid: string;
  cdek_number?: string | null;
  number?: string | null;
  recipient?: CdekOrderRecipient | null;
  statuses?: Array<{
    code?: string | null;
    name?: string | null;
    date_time?: string | null;
    city?: string | null;
  }>;
  packages?: Array<{
    number?: string | null;
    items?: Array<{
      name?: string | null;
      ware_key?: string | null;
      amount?: number | null;
      cost?: number | null;
    }>;
  }>;
};

export type CdekOrderResponse = {
  entity?: CdekOrderEntity | null;
  requests?: unknown[];
  related_entities?: unknown[];
};

export class CdekHttpError extends Error {
  constructor(
    readonly path: string,
    readonly status: number,
    readonly bodyText: string
  ) {
    super(`CDEK ${path} failed with ${status}: ${bodyText.slice(0, 500)}`);
    this.name = "CdekHttpError";
  }
}

export class CdekClient {
  private token: { accessToken: string; expiresAt: number } | null = null;

  constructor(
    private readonly baseUrl = config.CDEK_BASE_URL,
    private readonly clientId = config.CDEK_CLIENT_ID,
    private readonly clientSecret = config.CDEK_CLIENT_SECRET
  ) {}

  async getOrderByCdekNumber(cdekNumber: string): Promise<CdekOrderResponse> {
    return this.get(`/v2/orders?cdek_number=${encodeURIComponent(cdekNumber)}`);
  }

  async getOrderByImNumber(imNumber: string): Promise<CdekOrderResponse> {
    return this.get(`/v2/orders?im_number=${encodeURIComponent(imNumber)}`);
  }

  private async get<T>(path: string): Promise<T> {
    let { response, bodyText } = await this.requestWithToken(path);
    if (response.status === 401) {
      this.token = null;
      ({ response, bodyText } = await this.requestWithToken(path));
    }

    if (!response.ok) {
      throw new CdekHttpError(path, response.status, bodyText);
    }

    return parseJsonBody(bodyText, path) as T;
  }

  private async requestWithToken(path: string): Promise<{ response: Response; bodyText: string }> {
    const accessToken = await this.getAccessToken();
    const response = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        headers: {
          Authorization: `Bearer ${accessToken}`
        }
      },
      config.EXTERNAL_API_TIMEOUT_MS
    );
    return { response, bodyText: await response.text() };
  }

  private async getAccessToken(): Promise<string> {
    if (this.token && this.token.expiresAt > Date.now() + 60_000) {
      return this.token.accessToken;
    }

    if (!this.clientId || !this.clientSecret) {
      throw new Error("CDEK credentials are not configured");
    }

    const response = await fetchWithTimeout(
      `${this.baseUrl}/v2/oauth/token`,
      {
        method: "POST",
        headers: {
          "content-type": "application/x-www-form-urlencoded"
        },
        body: new URLSearchParams({
          grant_type: "client_credentials",
          client_id: this.clientId,
          client_secret: this.clientSecret
        })
      },
      config.EXTERNAL_API_TIMEOUT_MS
    );

    const bodyText = await response.text();
    if (!response.ok) {
      throw new CdekHttpError("/v2/oauth/token", response.status, bodyText);
    }

    const body = parseJsonBody(bodyText, "/v2/oauth/token") as { access_token?: string; expires_in?: number };
    if (!body.access_token) {
      throw new Error("CDEK token response does not contain access_token");
    }

    this.token = {
      accessToken: body.access_token,
      expiresAt: Date.now() + (body.expires_in ?? 3600) * 1000
    };

    return this.token.accessToken;
  }
}

function parseJsonBody(bodyText: string, path: string): unknown {
  if (!bodyText) {
    return {};
  }

  try {
    return JSON.parse(bodyText);
  } catch {
    throw new Error(`CDEK ${path} returned non-JSON response: ${bodyText.slice(0, 500)}`);
  }
}
