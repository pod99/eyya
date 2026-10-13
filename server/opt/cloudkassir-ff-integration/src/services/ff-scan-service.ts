import { config } from "../config.js";
import { FfPlatformClient, FfPlatformHttpError } from "../clients/ff-platform.js";
import { prisma } from "../db.js";
import { stringifyJson } from "../utils/json.js";
import { FfSyncService } from "./ff-sync-service.js";

const CURSOR_NAME = "default";

type ScanOneResult = "missing" | "saved" | "error" | "fatal";
type SyncOneResult = "saved" | "error" | "fatal";

export class FfScanService {
  constructor(
    private readonly ffClient = new FfPlatformClient(),
    private readonly ffSyncService = new FfSyncService(ffClient)
  ) {}

  async scanNextBatch() {
    if (config.FF_SCAN_MODE === "id_range") {
      return this.scanNextIdRangeBatch();
    }

    return this.scanCdekPeriodBatch();
  }

  private async scanCdekPeriodBatch() {
    const cursor = await this.ensureCursor();
    const period = buildScanPeriod();
    const statuses = parseScanStatuses(config.FF_SCAN_STATUSES);

    await prisma.ffScanCursor.update({
      where: { name: CURSOR_NAME },
      data: {
        lastStartedAt: new Date(),
        lastError: null
      }
    });

    try {
      const listedDispatches = await this.listPeriodDispatches(period.from, period.to, statuses);
      const allIds = uniqueSortedIds(listedDispatches.map((dispatch) => dispatch.id));
      const batch = selectPeriodBatch(allIds, cursor.nextSkladDispatchId, config.FF_SCAN_BATCH_SIZE);
      const ids = batch.ids;
      let nextIndex = 0;
      let scanned = 0;
      let saved = 0;
      let errors = 0;
      let fatalId: number | null = null;
      let lastError: string | null = null;
      const foundIds: number[] = [];

      const worker = async () => {
        while (fatalId === null) {
          const index = nextIndex++;
          const id = ids[index];
          if (id === undefined) {
            return;
          }

          const result = await this.syncListedDispatch(id);
          scanned += 1;

          if (result === "saved") {
            saved += 1;
            foundIds.push(id);
            continue;
          }

          if (result === "fatal") {
            fatalId = id;
            errors += 1;
            lastError = `Fatal FF period scan error at skladDispatchId=${id}`;
            return;
          }

          errors += 1;
        }
      };

      await Promise.all(Array.from({ length: config.FF_SCAN_CONCURRENCY }, () => worker()));

      const lastScannedId = ids.length > 0 ? Math.max(...ids) : cursor.lastScannedId;
      const lastFoundId = foundIds.length > 0 ? Math.max(cursor.lastFoundId ?? -1, ...foundIds) : cursor.lastFoundId;
      const nextSkladDispatchId = batch.nextStartId ?? cursor.nextSkladDispatchId;
      const updatedCursor = await prisma.ffScanCursor.update({
        where: { name: CURSOR_NAME },
        data: {
          nextSkladDispatchId,
          lastScannedId,
          lastFoundId,
          lastFinishedAt: new Date(),
          lastError
        }
      });

      return {
        mode: "cdek_period" as const,
        from: period.from,
        to: period.to,
        statuses,
        listed: listedDispatches.length,
        scanned,
        missing: 0,
        saved,
        errors,
        skipped: Math.max(0, allIds.length - ids.length),
        cursor: updatedCursor
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await prisma.ffScanCursor.update({
        where: { name: CURSOR_NAME },
        data: {
          lastFinishedAt: new Date(),
          lastError: message
        }
      });
      await this.recordScanError(`period:${period.from}:${period.to}`, error);
      throw error;
    }
  }

  private async scanNextIdRangeBatch() {
    const cursor = await prisma.ffScanCursor.upsert({
      where: { name: CURSOR_NAME },
      create: {
        name: CURSOR_NAME,
        nextSkladDispatchId: config.FF_SCAN_START_ID
      },
      update: {}
    });

    const startId = cursor.nextSkladDispatchId;
    const ids = Array.from({ length: config.FF_SCAN_BATCH_SIZE }, (_, index) => startId + index);
    await prisma.ffScanCursor.update({
      where: { name: CURSOR_NAME },
      data: {
        lastStartedAt: new Date(),
        lastError: null
      }
    });

    let nextIndex = 0;
    let scanned = 0;
    let missing = 0;
    let saved = 0;
    let errors = 0;
    let fatalId: number | null = null;
    let lastFoundId = cursor.lastFoundId;
    let lastError: string | null = null;
    const foundIds: number[] = [];

    const worker = async () => {
      while (fatalId === null) {
        const index = nextIndex++;
        const id = ids[index];
        if (id === undefined) {
          return;
        }

        const result = await this.scanOne(id);
        scanned += 1;

        if (result === "missing") {
          missing += 1;
          continue;
        }

        if (result === "saved") {
          saved += 1;
          foundIds.push(id);
          continue;
        }

        if (result === "fatal") {
          fatalId = id;
          errors += 1;
          lastError = `Fatal FF scan error at skladDispatchId=${id}`;
          return;
        }

        errors += 1;
      }
    };

    await Promise.all(Array.from({ length: config.FF_SCAN_CONCURRENCY }, () => worker()));

    const lastScannedId = fatalId === null ? startId + config.FF_SCAN_BATCH_SIZE - 1 : Math.max(startId, fatalId - 1);
    const nextSkladDispatchId = fatalId === null ? startId + config.FF_SCAN_BATCH_SIZE : fatalId;
    if (foundIds.length > 0) {
      lastFoundId = Math.max(lastFoundId ?? -1, ...foundIds);
    }

    const updatedCursor = await prisma.ffScanCursor.update({
      where: { name: CURSOR_NAME },
      data: {
        nextSkladDispatchId,
        lastScannedId,
        lastFoundId,
        lastFinishedAt: new Date(),
        lastError
      }
    });

    return {
      mode: "id_range" as const,
      startId,
      endId: startId + config.FF_SCAN_BATCH_SIZE - 1,
      scanned,
      missing,
      saved,
      errors,
      cursor: updatedCursor
    };
  }

  private async ensureCursor() {
    return prisma.ffScanCursor.upsert({
      where: { name: CURSOR_NAME },
      create: {
        name: CURSOR_NAME,
        nextSkladDispatchId: config.FF_SCAN_START_ID
      },
      update: {}
    });
  }

  private async listPeriodDispatches(from: string, to: string | undefined, statuses: string[]) {
    const dispatches = await Promise.all(
      statuses.map((status) =>
        this.ffClient.listCdekDispatches({
          from,
          to,
          status
        })
      )
    );

    return dispatches.flat();
  }

  private async syncListedDispatch(skladDispatchId: number): Promise<SyncOneResult> {
    try {
      await this.ffSyncService.syncSkladDispatch(skladDispatchId);
      return "saved";
    } catch (error) {
      await this.recordScanError(skladDispatchId, error);
      if (isFatalFfError(error)) {
        return "fatal";
      }

      return "error";
    }
  }

  private async scanOne(skladDispatchId: number): Promise<ScanOneResult> {
    try {
      const cdekDetail = await this.ffClient.getCdekDispatchDetail(skladDispatchId);
      if (!cdekDetail) {
        return "missing";
      }

      await this.ffSyncService.syncSkladDispatch(skladDispatchId);
      return "saved";
    } catch (error) {
      await this.recordScanError(skladDispatchId, error);
      if (isFatalFfError(error)) {
        return "fatal";
      }

      return "error";
    }
  }

  private async recordScanError(skladDispatchId: number | string, error: unknown): Promise<void> {
    await prisma.apiEvent.create({
      data: {
        source: "ff_platform",
        eventType: "scan_error",
        externalId: String(skladDispatchId),
        payloadJson: stringifyJson({
          skladDispatchId,
          message: error instanceof Error ? error.message : String(error),
          name: error instanceof Error ? error.name : undefined,
          status: error instanceof FfPlatformHttpError ? error.status : undefined
        })
      }
    });
  }
}

function isFatalFfError(error: unknown): boolean {
  if (!(error instanceof FfPlatformHttpError)) {
    return false;
  }

  return [401, 403, 429].includes(error.status);
}

function parseScanStatuses(rawStatuses: string): string[] {
  const statuses = rawStatuses
    .split(",")
    .map((status) => status.trim())
    .filter(Boolean);

  return statuses.length > 0 ? statuses : ["sended"];
}

function uniqueSortedIds(ids: number[]): number[] {
  return [...new Set(ids.filter((id) => Number.isInteger(id) && id > 0))].sort((left, right) => left - right);
}

function selectPeriodBatch(ids: number[], startAt: number, take: number): { ids: number[]; nextStartId?: number } {
  if (ids.length === 0) {
    return { ids: [] };
  }

  const startIndex = ids.findIndex((id) => id >= startAt);
  const orderedIds = startIndex === -1 ? ids : [...ids.slice(startIndex), ...ids.slice(0, startIndex)];
  const selectedIds = orderedIds.slice(0, take);

  if (selectedIds.length === 0) {
    return { ids: selectedIds, nextStartId: ids[0] };
  }

  if (selectedIds.length < ids.length) {
    return { ids: selectedIds, nextStartId: orderedIds[selectedIds.length] };
  }

  return { ids: selectedIds, nextStartId: Math.max(...ids) + 1 };
}

function buildScanPeriod(): { from: string; to?: string } {
  const from = normalizeFfDateInput(config.FF_SCAN_FROM) ?? formatFfDate(daysAgoInMoscow(config.FF_SCAN_LOOKBACK_DAYS));
  const to = normalizeFfDateInput(config.FF_SCAN_TO) ?? formatFfDate(nowInMoscow());
  return { from, to };
}

function normalizeFfDateInput(value: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) {
    return undefined;
  }

  const ffDate = /^(\d{2})-(\d{2})-(\d{4})$/.exec(trimmed);
  if (ffDate) {
    return trimmed;
  }

  const isoDate = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (isoDate) {
    return `${isoDate[3]}-${isoDate[2]}-${isoDate[1]}`;
  }

  return trimmed;
}

function daysAgoInMoscow(days: number): Date {
  return new Date(nowInMoscow().getTime() - days * 24 * 60 * 60 * 1000);
}

function nowInMoscow(): Date {
  return new Date(Date.now() + 3 * 60 * 60 * 1000);
}

function formatFfDate(date: Date): string {
  const day = String(date.getUTCDate()).padStart(2, "0");
  const month = String(date.getUTCMonth() + 1).padStart(2, "0");
  const year = date.getUTCFullYear();
  return `${day}-${month}-${year}`;
}
