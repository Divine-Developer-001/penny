/** Typed API client. Every call goes under /api (proxied to FastAPI in dev, same-origin in production). */
import type {
  CaptureResult, CreatedOrder, Deals, HistoryPoint, HistoryRange, PennyState, Product, ProposalAction, WithdrawResult,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = init.body ? { "Content-Type": "application/json" } : {};
  const res = await fetch(path, { ...init, headers });
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? (JSON.parse(text) as unknown) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(typeof detail === "string" ? detail : detail ? JSON.stringify(detail) : res.statusText, res.status);
  }
  return data as T;
}

const post = <T>(path: string, body?: unknown): Promise<T> =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

const RANGE_DAYS: Record<HistoryRange, number | null> = { "7d": 7, "30d": 30, all: null };

export const api = {
  getProducts: (q = ""): Promise<Product[]> => request<Product[]>(`/api/products?q=${encodeURIComponent(q)}`),
  getDeals: (productId: string): Promise<Deals> => request<Deals>(`/api/deals?product_id=${encodeURIComponent(productId)}`),
  createOrder: (productId: string, mode: "paypal" | "simulated"): Promise<CreatedOrder> =>
    post<CreatedOrder>("/api/orders", { product_id: productId, mode }),
  captureOrder: (orderId: string): Promise<CaptureResult> => post<CaptureResult>(`/api/orders/${encodeURIComponent(orderId)}/capture`),
  getState: (): Promise<PennyState> => request<PennyState>("/api/state"),
  getHistory: (range: HistoryRange): Promise<HistoryPoint[]> => {
    const days = RANGE_DAYS[range];
    return request<HistoryPoint[]>(days ? `/api/history?days=${days}` : "/api/history");
  },
  withdraw: (amount: string, includeInvestments: boolean): Promise<WithdrawResult> =>
    post<WithdrawResult>("/api/withdraw", { amount, include_investments: includeInvestments }),
  proposalAction: (id: number, action: "approve" | "reject" | "undo"): Promise<ProposalAction> =>
    post<ProposalAction>(`/api/proposals/${id}/${action}`),
  runAgent: (): Promise<{ mode: string; explanation: string }> => post<{ mode: string; explanation: string }>("/api/agent/run"),
};
