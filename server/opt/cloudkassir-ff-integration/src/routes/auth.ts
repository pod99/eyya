import { createHmac, timingSafeEqual } from "node:crypto";
import type { FastifyRequest } from "fastify";
import { config } from "../config.js";

const ADMIN_SESSION_COOKIE = "ckff_admin_session";
const ADMIN_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60;

export function isAdminAuthorized(request: FastifyRequest): boolean {
  if (isDashboardSessionAuthorized(request)) {
    return true;
  }
  if (isDashboardBasicAuthorized(request)) {
    return true;
  }

  if (!config.ADMIN_SECRET) {
    return config.NODE_ENV !== "production";
  }

  const headerSecret = request.headers["x-admin-secret"];
  const providedSecret = Array.isArray(headerSecret) ? headerSecret[0] : headerSecret;

  return timingSafeStringEqual(providedSecret, config.ADMIN_SECRET);
}

export function basicAuthChallenge(): string {
  return 'Basic realm="CloudKassir FF Dashboard"';
}

export function adminSessionCookieHeader(): string {
  return `${ADMIN_SESSION_COOKIE}=${adminSessionToken() ?? ""}; Path=/cloudkassir; HttpOnly; Secure; SameSite=Lax; Max-Age=${ADMIN_SESSION_MAX_AGE_SECONDS}`;
}

export function isDashboardCredentialsValid(username: string, password: string): boolean {
  if (!config.DASHBOARD_USER || !config.DASHBOARD_PASSWORD) {
    return false;
  }

  return timingSafeStringEqual(username, config.DASHBOARD_USER) && timingSafeStringEqual(password, config.DASHBOARD_PASSWORD);
}

export function isDashboardBasicAuthorized(request: FastifyRequest): boolean {
  const authorization = request.headers.authorization;
  if (!authorization?.startsWith("Basic ")) {
    return false;
  }

  const credentials = Buffer.from(authorization.slice("Basic ".length), "base64").toString("utf8");
  const separatorIndex = credentials.indexOf(":");
  if (separatorIndex === -1) {
    return false;
  }

  const username = credentials.slice(0, separatorIndex);
  const password = credentials.slice(separatorIndex + 1);

  return isDashboardCredentialsValid(username, password);
}

export function timingSafeStringEqual(actual: string | undefined, expected: string | undefined): boolean {
  if (actual === undefined || expected === undefined) {
    return false;
  }

  const actualBuffer = Buffer.from(actual);
  const expectedBuffer = Buffer.from(expected);
  return actualBuffer.length === expectedBuffer.length && timingSafeEqual(actualBuffer, expectedBuffer);
}

function isDashboardSessionAuthorized(request: FastifyRequest): boolean {
  const token = readCookie(request, ADMIN_SESSION_COOKIE);
  return timingSafeStringEqual(token, adminSessionToken());
}

function adminSessionToken(): string | undefined {
  const secret = config.ADMIN_SECRET || config.DASHBOARD_PASSWORD;
  if (!secret) {
    return undefined;
  }

  return createHmac("sha256", secret).update("cloudkassir-ff-dashboard-session-v1").digest("hex");
}

function readCookie(request: FastifyRequest, name: string): string | undefined {
  const cookieHeader = request.headers.cookie;
  if (!cookieHeader) {
    return undefined;
  }

  for (const pair of cookieHeader.split(";")) {
    const separatorIndex = pair.indexOf("=");
    if (separatorIndex === -1) {
      continue;
    }

    const key = pair.slice(0, separatorIndex).trim();
    if (key !== name) {
      continue;
    }

    return decodeURIComponent(pair.slice(separatorIndex + 1).trim());
  }

  return undefined;
}
