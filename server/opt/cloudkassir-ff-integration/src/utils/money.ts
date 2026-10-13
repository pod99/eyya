export function kopToRub(kop: number): number {
  return Number((kop / 100).toFixed(2));
}

export function rubToKop(rub: number): number {
  return Math.round(rub * 100);
}
