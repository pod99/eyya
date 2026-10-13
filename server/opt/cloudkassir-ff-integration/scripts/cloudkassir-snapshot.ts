import { prisma } from "../src/db.js";
import { CloudKassirImportService } from "../src/services/cloudkassir-import-service.js";

const service = new CloudKassirImportService();
const result = await service.fetchCashRegisterSnapshot();

console.log(JSON.stringify(result, null, 2));
await prisma.$disconnect();
