/** Root: the shop (StorePage) with the floating Penny widget mounted globally, plus a toast. */
import { useCallback, useEffect, useRef, useState } from "react";
import StorePage from "./store/StorePage";
import PennyWidget from "./widget/PennyWidget";

interface ToastState {
  text: string;
  error: boolean;
}

export default function App() {
  const [toast, setToast] = useState<ToastState | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [openKey, setOpenKey] = useState(0);
  const timer = useRef<number | undefined>(undefined);

  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setToast(null), 5200);
  }, []);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onPaid = useCallback((message: string) => {
    notify(message);
    setRefreshKey((k) => k + 1);   // refresh the widget right away
    setOpenKey((k) => k + 1);      // and pop it open so the cashback is visible
  }, [notify]);

  return (
    <>
      <StorePage onPaid={onPaid} notify={notify} />
      <PennyWidget refreshKey={refreshKey} openKey={openKey} notify={notify} />
      {toast && <div className={`toast${toast.error ? " err" : ""}`} role="status">{toast.text}</div>}
    </>
  );
}
