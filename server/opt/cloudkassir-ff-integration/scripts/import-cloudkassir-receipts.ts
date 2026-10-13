import { readFile } from "node:fs/promises";
import { prisma } from "../src/db.js";
import { CloudKassirImportService } from "../src/services/cloudkassir-import-service.js";

const args = parseArgs(process.argv.slice(2));
const ids = await resolveIds(args);

if (ids.length === 0) {
  console.error("Usage: npm run import:cloudkassir:receipts -- --ids id1,id2 OR --file ./receipt-ids.txt");
  process.exit(1);
}

const service = new CloudKassirImportService();
const result = await service.importReceiptsByIds(ids);

console.log(JSON.stringify({
  requested: result.requested,
  imported: result.imported,
  failed: result.requested - result.imported
}, null, 2));
await prisma.$disconnect();

async function resolveIds(args: Record<string, string>) {
  if (args.ids) {
    return splitIds(args.ids);
  }

  if (args.file) {
    const text = await readFile(args.file, "utf8");
    return splitIds(text);
  }

  return [];
}

function splitIds(value: string) {
  return value
    .split(/[\n,;\s]+/)
    .map((id) => id.trim())
    .filter(Boolean);
}

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
