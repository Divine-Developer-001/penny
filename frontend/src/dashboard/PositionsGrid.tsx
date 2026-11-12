/** Renders the holdings as an AG Grid table: ticker, shares, avg cost, current value, P/L ($ and %), share of portfolio. */
import { useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import type { ColDef } from "ag-grid-community";
import { money, pct, signedMoney } from "../format";
import { compactTheme } from "./gridTheme";
import type { Holding } from "../types";

const plStyle = (v: number | undefined) => ({ color: (v ?? 0) >= 0 ? "#0f9d58" : "#d93025", fontWeight: 600 });

export default function PositionsGrid({ holdings }: { holdings: Holding[] }) {
  const rows = useMemo(
    () => holdings.map((h) => ({
      ticker: h.ticker, shares: Number(h.quantity), avg: Number(h.avg_cost), value: Number(h.value),
      pl: Number(h.profit_loss), plPct: Number(h.profit_loss_pct), weight: Number(h.weight) * 100,
    })),
    [holdings],
  );
  type Row = (typeof rows)[number];
  const cols = useMemo<ColDef<Row>[]>(() => [
    { field: "ticker", headerName: "Fund", minWidth: 42, cellStyle: { fontWeight: 700 } },
    { field: "shares", headerName: "Shares", minWidth: 50, valueFormatter: (p) => Number(p.value).toFixed(3) },
    { field: "avg", headerName: "Avg cost", minWidth: 56, valueFormatter: (p) => money(p.value as number) },
    { field: "value", headerName: "Value", minWidth: 52, valueFormatter: (p) => money(p.value as number) },
    {
      colId: "pl", headerName: "P/L", minWidth: 108, flex: 1.8, valueGetter: (p) => p.data?.pl ?? 0,
      valueFormatter: (p) => `${signedMoney(p.value as number)} (${pct(p.data?.plPct ?? 0, 1)})`, cellStyle: (p) => plStyle(p.value as number),
    },
    { field: "weight", headerName: "%", headerTooltip: "% of portfolio", minWidth: 40, valueFormatter: (p) => `${(p.value as number).toFixed(0)}%` },
  ], []);
  if (rows.length === 0) {
    return <div className="muted small">Nothing invested yet. Penny invests once the pot reaches the threshold above.</div>;
  }
  return (
    <div className="grid-wrap">
      <AgGridReact<Row> rowData={rows} columnDefs={cols} theme={compactTheme} domLayout="autoHeight" suppressCellFocus suppressHorizontalScroll
        autoSizeStrategy={{ type: "fitGridWidth" }} defaultColDef={{ sortable: true, resizable: true, flex: 1 }} getRowId={(p) => p.data.ticker} />
    </div>
  );
}
