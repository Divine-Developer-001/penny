/** Renders the floating Penny button (bottom-right, pot badge) and the slide-up dashboard panel.
 *  Polls GET /api/state every 5s, pausing while the tab is hidden. */
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { money } from "../format";
import type { PennyState } from "../types";
import PennyAvatar from "./PennyAvatar";

interface Props {
  refreshKey: number;
  openKey: number;
  notify: (text: string, error?: boolean) => void;
}

const POLL_MS = 5000;
const Dashboard = lazy(() => import("../dashboard/Dashboard"));     // AG Grid + AG Charts load only when Penny is opened

export default function PennyWidget({ refreshKey, openKey, notify }: Props) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<PennyState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pulse, setPulse] = useState(false);
  const lastCashback = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.getState();
      const cashback = Number(next.totals.cashback_total);
      if (lastCashback.current !== null && cashback > lastCashback.current) {
        setPulse(true);
        window.setTimeout(() => setPulse(false), 2600);
      }
      lastCashback.current = cashback;
      setState(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not reach the server");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    const onVisible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(id); document.removeEventListener("visibilitychange", onVisible); };
  }, [refresh]);

  useEffect(() => { if (refreshKey > 0) { void refresh(); window.setTimeout(() => void refresh(), 3500); } }, [refreshKey, refresh]);
  useEffect(() => { if (openKey > 0) setOpen(true); }, [openKey]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      <button className={`fab${pulse ? " pulse" : ""}`} onClick={() => setOpen((o) => !o)} aria-label={open ? "Close Penny dashboard" : "Open Penny dashboard"} aria-expanded={open}>
        {state && Number(state.pot.available) > 0 && <span className="badge">{money(state.pot.available)}</span>}
        <PennyAvatar size={64} />
      </button>
      {open && (
        <aside className="panel" aria-label="Penny dashboard">
          <div className="ph">
            <PennyAvatar size={36} />
            <div className="grow">
              <b>Penny</b>
              {state && <span className={`pill ${state.broker.simulated ? "warn" : "ok"}`}>{state.broker.label}</span>}
            </div>
            <button className="close" onClick={() => setOpen(false)} aria-label="Close">×</button>
          </div>
          <div className="banner">Simulated cashback • Paper trading • Not financial advice.</div>
          {state ? (
            <Suspense fallback={<div className="sec muted">Loading dashboard…</div>}>
              <Dashboard state={state} onChanged={refresh} notify={notify} />
            </Suspense>
          ) : <div className="sec muted">{error ?? "Loading…"}</div>}
          {error && state && <div className="sec err-text">Connection problem: {error}</div>}
        </aside>
      )}
    </>
  );
}
