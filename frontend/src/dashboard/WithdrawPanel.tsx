/** Renders the withdraw form (amount, optional "include my investments") and the live payout status line. */
import { useState } from "react";
import { api } from "../api";
import { money } from "../format";
import type { PennyState } from "../types";

interface Props {
  state: PennyState;
  onChanged: () => Promise<void>;
  notify: (text: string, error?: boolean) => void;
}

const STATUS_TEXT = { CREATED: "Starting…", IN_TRANSIT: "In transit to PayPal…", COMPLETED: "Completed", FAILED: "Failed (money returned to your pot)" } as const;

export default function WithdrawPanel({ state, onChanged, notify }: Props) {
  const [amount, setAmount] = useState("");
  const [include, setInclude] = useState(false);
  const [busy, setBusy] = useState(false);
  const t = state.totals;
  const cashOnly = Math.max(Number(t.max_withdrawable) - Number(t.positions_value), 0);

  const submit = async () => {
    const n = Number(amount);
    if (!(n > 0)) { notify("Enter an amount to withdraw.", true); return; }
    setBusy(true);
    try {
      const r = await api.withdraw(n.toFixed(2), include);
      notify(`Withdrawing ${money(r.amount)}. ${r.message}`);
      setAmount("");
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
      await onChanged();
    }
  };

  const p = state.latest_payout;
  return (
    <div className="sec">
      <h4>Withdraw</h4>
      <div className="wd">
        <div className="row">
          <input type="number" min="0.01" step="0.01" value={amount} placeholder="Amount (USD)" aria-label="Withdraw amount" onChange={(e) => setAmount(e.target.value)} />
          <button className="mini" onClick={() => setAmount((include ? Number(t.max_withdrawable) : cashOnly).toFixed(2))}>Max</button>
        </div>
        <label className="check">
          <input type="checkbox" checked={include} onChange={(e) => setInclude(e.target.checked)} />
          <span>Include my investments (Penny sells some holdings pro rata to raise the cash)</span>
        </label>
        <button className="btn" onClick={() => void submit()} disabled={busy}>{busy ? "Working…" : "Withdraw to PayPal"}</button>
        {p && (
          <div className={`payout ${p.status.toLowerCase()}`}>
            Latest payout {money(p.amount)}: <b>{STATUS_TEXT[p.status]}</b>
          </div>
        )}
        <div className="fine">Payouts go to a PayPal <b>sandbox</b> account. Cash in the pot is withdrawn first.</div>
      </div>
    </div>
  );
}
