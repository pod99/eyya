import "dotenv/config";
import { z } from "zod";

const configSchema = z.object({
  NODE_ENV: z.string().default("development"),
  PORT: z.coerce.number().int().positive().default(3100),
  DATABASE_URL: z.string().default("file:./dev.db"),
  DRY_RUN: z
    .string()
    .default("true")
    .transform((value) => value.toLowerCase() === "true"),
  CLOUDKASSIR_BASE_URL: z.string().url().default("https://api.cloudpayments.ru"),
  CLOUDKASSIR_PUBLIC_ID: z.string().optional().default(""),
  CLOUDKASSIR_API_SECRET: z.string().optional().default(""),
  CLOUDKASSIR_INN: z.string().optional().default(""),
  CLOUDKASSIR_TAXATION_SYSTEM: z.coerce.number().int().min(0).max(5).default(1),
  CLOUDKASSIR_CALCULATION_PLACE: z.string().default(""),
  CLOUDKASSIR_RUSSIA_TIME_ZONE: z.coerce.number().int().min(1).max(11).default(2),
  FF_PLATFORM_BASE_URL: z.string().url().default("https://ff-platform.ru/lk/1"),
  FF_PLATFORM_TOKEN: z.string().optional().default(""),
  FF_SCAN_ENABLED: z
    .string()
    .default("true")
    .transform((value) => value.toLowerCase() === "true"),
  FF_SCAN_MODE: z.enum(["cdek_period", "id_range"]).default("cdek_period"),
  FF_SCAN_START_ID: z.coerce.number().int().min(0).default(0),
  FF_SCAN_BATCH_SIZE: z.coerce.number().int().positive().max(2000).default(300),
  FF_SCAN_CONCURRENCY: z.coerce.number().int().positive().max(20).default(10),
  FF_SCAN_INTERVAL_MS: z.coerce.number().int().positive().default(60_000),
  FF_SCAN_LOOKBACK_DAYS: z.coerce.number().int().positive().max(90).default(3),
  FF_SCAN_FROM: z.string().optional().default(""),
  FF_SCAN_TO: z.string().optional().default(""),
  FF_SCAN_STATUSES: z.string().default("sended"),
  WORKER_POLL_INTERVAL_MS: z.coerce.number().int().positive().default(60_000),
  CLOUDKASSIR_POLL_ENABLED: z
    .string()
    .default("true")
    .transform((value) => value.toLowerCase() === "true"),
  CLOUDKASSIR_POLL_INTERVAL_MS: z.coerce.number().int().positive().default(30 * 60_000),
  CLOUDKASSIR_POLL_LOOKBACK_MINUTES: z.coerce.number().int().positive().default(60),
  CLOUDKASSIR_POLL_TIME_ZONE: z.string().default("MSK"),
  CLOUDKASSIR_POLL_MAX_PAGES: z.coerce.number().int().positive().default(20),
  CLOUDKASSIR_POLL_RECEIPT_PAGE_LIMIT: z.coerce.number().int().positive().max(500).default(100),
  CLOUDKASSIR_POLL_KNOWN_RECEIPT_LIMIT: z.coerce.number().int().positive().default(200),
  EXTERNAL_API_TIMEOUT_MS: z.coerce.number().int().positive().default(30_000),
  ORDER_RECONSTRUCT_ENABLED: z
    .string()
    .default("true")
    .transform((value) => value.toLowerCase() === "true"),
  ORDER_RECONSTRUCT_BATCH_SIZE: z.coerce.number().int().positive().max(200).default(50),
  ORDER_RECONSTRUCT_DATE_FROM: z.string().optional().default(""),
  ORDER_RECONSTRUCT_DATE_TO: z.string().optional().default(""),
  CDEK_BASE_URL: z.string().url().default("https://api.cdek.ru"),
  CDEK_CLIENT_ID: z.string().optional().default(""),
  CDEK_CLIENT_SECRET: z.string().optional().default(""),
  CDEK_ENRICH_ENABLED: z
    .string()
    .default("true")
    .transform((value) => value.toLowerCase() === "true"),
  CDEK_ENRICH_INTERVAL_MS: z.coerce.number().int().positive().default(5 * 60_000),
  CDEK_ENRICH_BATCH_SIZE: z.coerce.number().int().positive().max(200).default(50),
  CLOUDKASSIR_WEBHOOK_SECRET: z.string().optional().default(""),
  ADMIN_SECRET: z.string().optional().default(""),
  DASHBOARD_USER: z.string().optional().default("admin"),
  DASHBOARD_PASSWORD: z.string().optional().default(""),
  FISCAL_RECEIPT_SENDING_TIMEOUT_MS: z.coerce.number().int().positive().default(10 * 60_000),
  SECOND_RECEIPT_RETRY_LATER_MINUTES: z.coerce.number().int().positive().default(360)
});

export const config = configSchema.parse(process.env);

export function assertReadyForLiveCloudKassir(): void {
  if (config.DRY_RUN) {
    return;
  }

  const missing = [
    ["CLOUDKASSIR_PUBLIC_ID", config.CLOUDKASSIR_PUBLIC_ID],
    ["CLOUDKASSIR_API_SECRET", config.CLOUDKASSIR_API_SECRET],
    ["CLOUDKASSIR_INN", config.CLOUDKASSIR_INN]
  ].filter(([, value]) => !value);

  if (missing.length > 0) {
    throw new Error(
      `CloudKassir live mode is missing env vars: ${missing.map(([key]) => key).join(", ")}`
    );
  }
}
