/** Lays out the dashboard sections inside the widget panel: pot hero, invested, performance, orders, activity, withdraw. */
import "./agSetup";
import type { PennyState } from "../types";
import ActivityFeed from "./ActivityFeed";
import ApprovalsList from "./ApprovalsList";
import PerformanceChart from "./PerformanceChart";
import PositionsGrid from "./PositionsGrid";
import PotHero from "./PotHero";
import WithdrawPanel from "./WithdrawPanel";

interface Props {
  state: PennyState;
  onChanged: () => Promise<void>;
  notify: (text: string, error?: boolean) => void;
}

export default function Dashboard({ state, onChanged, notify }: Props) {
  return (
    <>
      <PotHero state={state} />
      <ApprovalsList pending={state.pending_approvals} queued={state.queued_orders} onChanged={onChanged} notify={notify} />
      <div className="sec"><h4>Invested</h4><PositionsGrid holdings={state.holdings} /></div>
      <PerformanceChart refreshKey={state.recent_proposals.length + state.holdings.length} />
      <div className="sec"><h4>Activity</h4><ActivityFeed activity={state.activity} /></div>
      <WithdrawPanel state={state} onChanged={onChanged} notify={notify} />
      <div className="sec fine">{state.disclaimer}</div>
    </>
  );
}
