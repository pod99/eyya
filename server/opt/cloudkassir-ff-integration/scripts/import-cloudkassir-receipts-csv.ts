import { readFile } from "node:fs/promises";
import { prisma } from "../src/db.js";
import { CloudKassirImportService } from "../src/services/cloudkassir-import-service.js";
import { rubToKop } from "../src/utils/money.js";

const args = parseArgs(process.argv.slice(2));

if (!args.file) {
  console.error("Usage: npm run import:cloudkassir:receipts-csv -- --file /path/to/cloudkassir-export.csv");
  process.exit(1);
}

const csv = await readCsv(args.file);
const rows = parseTsv(csv);
const header = rows[0] ?? [];
const dataRows = rows.slice(1).filter((row) => row.some((cell) => cell.trim() !== ""));
const idIndex = findColumn(header, "Идентификатор");
const amountIndex = findColumn(header, "Сумма");

if (idIndex === -1) {
  throw new Error("CSV export does not contain `Идентификатор` column");
}

const ids = unique(dataRows.map((row) => row[idIndex]).filter(Boolean));
const csvAmountKop =
  amountIndex === -1
    ? null
    : dataRows.reduce((sum, row) => sum + parseRussianMoneyToKop(row[amountIndex]), 0);

const service = new CloudKassirImportService();
const result = await service.importReceiptsByIds(ids);

const summary = {
  file: args.file,
  rows: dataRows.length,
  uniqueReceiptIds: ids.length,
  csvAmountRub: csvAmountKop === null ? null : Number((csvAmountKop / 100).toFixed(2)),
  requested: result.requested,
  imported: result.imported,
  failed: result.requested - result.imported
};

await prisma.apiEvent.create({
  data: {
    source: "cloudkassir",
    eventType: "receipt_csv_import_result",
    externalId: args.file,
    payloadJson: JSON.stringify(summary)
  }
});

console.log(JSON.stringify(summary, null, 2));
await prisma.$disconnect();

async function readCsv(path: string): Promise<string> {
  const bytes = await readFile(path);
  const hasUtf16LeBom = bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe;
  const hasZeroBytes = bytes.subarray(0, Math.min(bytes.length, 200)).includes(0);

  if (hasUtf16LeBom || hasZeroBytes) {
    return new TextDecoder("utf-16le").decode(bytes).replace(/^\uFEFF/, "");
  }

  return new TextDecoder("utf-8").decode(bytes).replace(/^\uFEFF/, "");
}

function parseTsv(text: string): string[][] {
  return text
    .split(/\r?\n/)
    .map((line) => line.split("\t").map((cell) => cell.trim()));
}

function findColumn(header: string[], name: string): number {
  return header.findIndex((column) => column.replace(/^\uFEFF/, "").trim() === name);
}

function unique(values: string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}

function parseRussianMoneyToKop(value: string | undefined): number {
  if (!value) {
    return 0;
  }

  const normalized = value.replace(/\s|\u00A0/g, "").replace(",", ".");
  const number = Number(normalized);

  return Number.isFinite(number) ? rubToKop(number) : 0;
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
