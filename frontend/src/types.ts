/** API types mirroring the FastAPI schemas. Decimals travel as strings; convert with Number() only for display. */
export type Decimal = string;

export interface Product {
  id: string;
  name: string;
  description: string;
  image_url: string;
  price: Decimal;
  category: string;
}

export interface DealOffer {
  title: string;
  merchant: string;
  price: Decimal;
  delta: Decimal;
  link: string | null;
}

export interface Deals {
  product_id: string;
  store_price: Decimal;
  sample: boolean;
  offers: DealOffer[];
  note: string | null;
}

export interface CreatedOrder {
  order_id: string;
  mode: string;
  amount: Decimal;
  merchant: string;
  product_name: string;
  cashback_rate: Decimal;
}

export interface CaptureResult {
  status: string;
  amount: Decimal;
  cashback: Decimal;
  cashback_rate: Decimal;
  message: string;
  duplicate: boolean;
}

export interface Broker {
  mode: string;
  label: string;
  simulated: boolean;
}

export interface Pot {
  available: Decimal;
  in_transit: Decimal;
  committed: Decimal;
  uncommitted: Decimal;
}

export interface Totals {
  cashback_total: Decimal;
  withdrawn: Decimal;
  cash: Decimal;
  in_transit: Decimal;
  positions_value: Decimal;
  total_value: Decimal;
  profit_loss: Decimal;
  profit_loss_pct: Decimal;
  max_withdrawable: Decimal;
}

export interface Holding {
  ticker: string;
  name: string;
  quantity: Decimal;
  avg_cost: Decimal;
  price: Decimal | null;
  value: Decimal;
  weight: Decimal;
  target_weight: Decimal | null;
  profit_loss: Decimal;
  profit_loss_pct: Decimal;
}

export interface ActivityItem {
  kind: string;
  description: string;
  created_at: string;
}

export interface Proposal {
  id: number;
  ticker: string;
  dollar_amount: Decimal;
  status: string;
  policy_reason: string;
  llm_rationale: string;
  created_at: string;
  executed_at: string | null;
  undo_deadline: string | null;
}

export interface Payout {
  id: number;
  amount: Decimal;
  status: "CREATED" | "IN_TRANSIT" | "COMPLETED" | "FAILED";
  detail: string;
  created_at: string;
}

export interface PennyState {
  broker: Broker;
  profile: string;
  pot: Pot;
  totals: Totals;
  holdings: Holding[];
  activity: ActivityItem[];
  queued_orders: Proposal[];
  pending_approvals: Proposal[];
  recent_proposals: Proposal[];
  latest_payout: Payout | null;
  status_message: string;
  invest_threshold: Decimal;
  disclaimer: string;
}

export interface HistoryPoint {
  t: string;
  value: Decimal;
  invested_cost: Decimal;
}

export interface WithdrawResult {
  payout_id: number;
  status: string;
  amount: Decimal;
  message: string;
}

export interface ProposalAction {
  proposal: Proposal;
  message: string;
}

export type HistoryRange = "7d" | "30d" | "all";
