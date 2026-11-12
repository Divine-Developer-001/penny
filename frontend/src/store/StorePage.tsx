/** Renders the product grid. Each card has a PayPal sandbox button (create + capture through the backend)
 *  and a lazily loaded "Penny found better deals" list (Channel3, or labelled sample offers). */
import { useCallback, useEffect, useState } from "react";
import { PayPalButtons, PayPalScriptProvider } from "@paypal/react-paypal-js";
import { api } from "../api";
import { money } from "../format";
import type { CaptureResult, Deals, Product } from "../types";
import DealCard from "./DealCard";

interface Props {
  onPaid: (message: string) => void;
  notify: (text: string, error?: boolean) => void;
}

const CLIENT_ID = (import.meta.env.VITE_PAYPAL_CLIENT_ID as string | undefined) ?? "";
const PAYPAL_READY = CLIENT_ID !== "" && !/demo|replace/i.test(CLIENT_ID);      // the sandbox client id is public by design
const QUICK = ["running", "headphones", "coffee", "backpack", "yoga", "keyboard"];

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export default function StorePage({ onPaid, notify }: Props) {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [query, setQuery] = useState("");
  const [deals, setDeals] = useState<Record<string, Deals | "loading">>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    (q: string) => {
      api.getProducts(q).then(setProducts).catch((e: unknown) => notify(errorText(e), true));
    },
    [notify],
  );
  useEffect(() => load(""), [load]);

  const finish = useCallback((r: CaptureResult) => onPaid(r.message), [onPaid]);

  const loadDeals = async (p: Product) => {
    setDeals((d) => ({ ...d, [p.id]: "loading" }));
    try {
      const result = await api.getDeals(p.id);
      setDeals((d) => ({ ...d, [p.id]: result }));
    } catch (e) {
      setDeals((d) => {
        const { [p.id]: _removed, ...rest } = d;
        return rest;
      });
      notify(errorText(e), true);
    }
  };

  const demoPay = async (p: Product) => {
    setBusy(p.id);
    try {
      const order = await api.createOrder(p.id, "simulated");
      finish(await api.captureOrder(order.order_id));
    } catch (e) {
      notify(errorText(e), true);
    } finally {
      setBusy(null);
    }
  };

  const grid = (
    <section className="grid" aria-live="polite">
      {products === null && <div className="empty">Loading products…</div>}
      {products?.length === 0 && <div className="empty">No products match “{query}”.</div>}
      {products?.map((p) => {
        const d = deals[p.id];
        return (
          <article className="card" key={p.id}>
            <img className="pic" src={p.image_url} alt={p.name} loading="lazy" />
            <div className="body">
              <span className="cat">{p.category}</span>
              <h3>{p.name}</h3>
              <div className="desc">{p.description}</div>
              <div className="price">{money(p.price)}</div>

              {PAYPAL_READY ? (
                <div className="pp">
                  <PayPalButtons
                    style={{ layout: "horizontal", shape: "pill", label: "pay", height: 36, tagline: false }}
                    fundingSource="paypal"
                    createOrder={async () => (await api.createOrder(p.id, "paypal")).order_id}
                    onApprove={async (data) => {
                      try {
                        finish(await api.captureOrder(data.orderID));
                      } catch (e) {
                        notify(errorText(e), true);
                      }
                    }}
                    onCancel={() => notify("Payment cancelled.")}
                    onError={(err) => notify(`PayPal error: ${errorText(err)}`, true)}
                  />
                </div>
              ) : null}
              <button className={PAYPAL_READY ? "linkish" : "btn"} disabled={busy === p.id} onClick={() => void demoPay(p)}>
                {PAYPAL_READY ? "or pay without PayPal (demo)" : "Buy (demo checkout)"}
              </button>

              <button className="btn ghost" onClick={() => void loadDeals(p)} disabled={d === "loading"}>
                {d === "loading" ? "Searching…" : "Find better deals"}
              </button>
              {d && d !== "loading" && (
                <div className="deals">
                  {d.sample && <div className="fine">{d.note ?? "Sample offers (no Channel3 key configured)."}</div>}
                  {d.offers.map((o, i) => <DealCard key={`${o.merchant}-${i}`} offer={o} sample={d.sample} />)}
                </div>
              )}
            </div>
          </article>
        );
      })}
    </section>
  );

  return (
    <>
      <header className="top">
        <div className="brand"><span className="brand-dot">P</span> Penny Shop</div>
        <form className="search" onSubmit={(e) => { e.preventDefault(); load(query.trim()); }} role="search">
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search products, e.g. running, coffee" aria-label="Search products" />
          <button className="btn" type="submit">Search</button>
        </form>
        <span className={`pill ${PAYPAL_READY ? "ok" : "warn"}`}>{PAYPAL_READY ? "PayPal sandbox" : "PayPal not configured"}</span>
      </header>
      <p className="notice">
        Demo store. Payments run in the PayPal <b>sandbox</b> (no real money). <b>Simulated cashback • Paper trading • Not financial advice.</b>
      </p>
      <main>
        <section className="hero">
          <h1>Shop smart. Let Penny grow your cashback.</h1>
          <p>Every purchase earns a little cashback. Penny pools the small amounts nobody notices and invests them in a simple paper portfolio. Click Penny in the corner anytime.</p>
        </section>
        <div className="chips">
          {QUICK.map((q) => (
            <button key={q} className="chip" onClick={() => { setQuery(q); load(q); }}>{q}</button>
          ))}
        </div>
        {PAYPAL_READY ? (
          <PayPalScriptProvider options={{ clientId: CLIENT_ID, currency: "USD", intent: "capture", components: "buttons" }}>{grid}</PayPalScriptProvider>
        ) : (
          grid
        )}
      </main>
    </>
  );
}
