/** Small formatting helpers shared by the store and the dashboard. */
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export const money = (v: string | number): string => usd.format(Number(v));
export const signedMoney = (v: string | number): string => `${Number(v) >= 0 ? "+" : "−"}${usd.format(Math.abs(Number(v)))}`;
export const pct = (v: string | number, digits = 2): string => `${Number(v) >= 0 ? "+" : "−"}${Math.abs(Number(v)).toFixed(digits)}%`;

/** The API returns naive UTC timestamps; treat them as UTC. */
export const parseUtc = (s: string): Date => new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : `${s}Z`);
export const timeLabel = (s: string): string =>
  parseUtc(s).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
