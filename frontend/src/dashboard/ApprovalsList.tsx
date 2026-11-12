/** Renders orders needing a decision: Approve/Reject for ones above the cap, and Undo (with a countdown) for approved ones. */
import { useEffect, useState } from "react";
import { api } from "../api";
import { money, parseUtc } from "../format";
import type { Proposal } from "../types";

interface Props {
  pending: Proposal[];
  queued: Proposal[];
  onChanged: () => Promise<void>;
  notify: (text: string, error?: boolean) => void;
}

export default function ApprovalsList({ pending, queued, onChanged, notify }: Props) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (queued.length === 0) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [queued.length]);

  const act = async (id: number, action: "approve" | "reject" | "undo") => {
    try {
      const r = await api.proposalAction(id, action);
      notify(r.message);
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e), true);
    }
    await onChanged();
  };

  if (pending.length === 0 && queued.length === 0) return null;
  return (
    <div className="sec">
      <h4>Orders</h4>
      {pending.map((o) => (
        <div className="order" key={o.id}>
          <span className="grow">Approve <b>{money(o.dollar_amount)}</b> of <b>{o.ticker}</b>? (above your auto-approve cap)</span>
          <button className="mini" onClick={() => void act(o.id, "approve")}>Approve</button>
          <button className="mini" onClick={() => void act(o.id, "reject")}>Reject</button>
        </div>
      ))}
      {queued.map((o) => {
        const secs = o.undo_deadline ? Math.max(0, Math.round((parseUtc(o.undo_deadline).getTime() - now) / 1000)) : 0;
        return (
          <div className="order" key={o.id}>
            <span className="grow">Buying <b>{money(o.dollar_amount)}</b> of <b>{o.ticker}</b> {secs > 0 ? `in ${secs}s` : "now"}</span>
            <button className="mini" onClick={() => void act(o.id, "undo")}>Undo</button>
          </div>
        );
      })}
    </div>
  );
}
