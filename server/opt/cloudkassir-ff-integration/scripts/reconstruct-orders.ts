import { prisma } from "../src/db.js";
import { OrderReconstructionService } from "../src/services/order-reconstruction-service.js";

const args = parseArgs(process.argv.slice(2));
const service = new OrderReconstructionService();
const result = await service.reconstructPendingOrders({
  take: args.take ? Number(args.take) : undefined,
  receiptDateFrom: args.from,
  receiptDateTo: args.to
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
