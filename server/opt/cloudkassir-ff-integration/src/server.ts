import { parse as parseQueryString } from "node:querystring";
import cors from "@fastify/cors";
import Fastify from "fastify";
import { config } from "./config.js";
import { logger } from "./logger.js";
import { registerAdminRoutes } from "./routes/admin.js";
import { registerCloudKassirImportRoutes } from "./routes/cloudkassir-import.js";
import { registerDashboardRoutes } from "./routes/dashboard.js";
import { registerFfRoutes } from "./routes/ff.js";
import { registerOrderRoutes } from "./routes/orders.js";
import { registerReceiptRoutes } from "./routes/receipts.js";
import { registerWebhookRoutes } from "./routes/webhooks.js";

const app = Fastify({
  loggerInstance: logger
});

app.addContentTypeParser("application/x-www-form-urlencoded", { parseAs: "string" }, (_request, body, done) => {
  done(null, parseQueryString(String(body)));
});

await app.register(cors, {
  origin: true
});

app.get("/health", async () => {
  return {
    ok: true,
    dryRun: config.DRY_RUN
  };
});

app.get("/", async (_request, reply) => {
  return reply.redirect("/dashboard");
});

await app.register(registerOrderRoutes);
await app.register(registerFfRoutes);
await app.register(registerReceiptRoutes);
await app.register(registerCloudKassirImportRoutes);
await app.register(registerAdminRoutes);
await app.register(registerWebhookRoutes);
await app.register(registerDashboardRoutes);

await app.listen({
  port: config.PORT,
  host: "0.0.0.0"
});
