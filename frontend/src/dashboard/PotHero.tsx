/** Renders the headline numbers: total value and P/L, cash waiting, lifetime cashback, and "Penny invests at $X, $Y to go". */
import { money, pct, signedMoney } from "../format";
import type { PennyState } from "../types";

export default function PotHero({ state }: { state: PennyState }) {
  const t = state.totals;
  const up = Number(t.profit_loss) >= 0;
  const threshold = Number(state.invest_threshold);
  const cash = Number(state.pot.uncommitted);
  const togo = Math.max(threshold - cash, 0);
  const progress = threshold > 0 ? Math.min(cash / threshold, 1) * 100 : 0;
  return (
    <div className="sec">
      <h4>You have now</h4>
      <div className="big">{money(t.total_value)}</div>
      <div className={`pl ${up ? "up" : "down"}`}>
        {signedMoney(t.profit_loss)} ({pct(t.profit_loss_pct)}) since you started
      </div>
      <div className="stats">
        <div className="stat"><small>Cash waiting (the pot)</small><b>{money(t.cash)}</b></div>
        <div className="stat"><small>Lifetime cashback</small><b>{money(t.cashback_total)}</b></div>
      </div>
      <div className="progress-label">
        {togo > 0 ? <>Penny invests at <b>{money(threshold)}</b> — <b>{money(togo)}</b> to go</> : <>Ready to invest: <b>{money(cash)}</b> is over the {money(threshold)} threshold</>}
      </div>
      <div className="bar"><i style={{ width: `${progress}%` }} /></div>
      <div className="says"><span className="says-dot" />{state.status_message}</div>
    </div>
  );
}
