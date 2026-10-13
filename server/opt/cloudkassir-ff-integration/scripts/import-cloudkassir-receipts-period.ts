import { prisma } from "../src/db.js";
import { CloudKassirImportService } from "../src/services/cloudkassir-import-service.js";

const args = parseArgs(process.argv.slice(2));

if (!args.from || !args.to) {
  console.error(
    "Usage: npm run import:cloudkassir:receipts-period -- --from 2026-06-27T00:00:00+03:00 --to 2026-06-27T23:59:59+03:00"
  );
  process.exit(1);
}

const service = new CloudKassirImportService();
const result = await service.importReceiptsByFiscalPeriod({
  from: args.from,
  to: args.to,
  limit: args.limit ? Number(args.limit) : undefined,
  maxPages: args.maxPages ? Number(args.maxPages) : undefined
});

console.log(JSON.stringify(result, null, 2));
await prisma.$disconnect();

function parseArgs(argv: string[]) {
  const result: Record<string, string> = {};

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      continue;
    }

    const key = arg.slice(2);
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      result[key] = "true";
      continue;
    }

    result[key] = value;
    index += 1;
  }

  return result;
}
