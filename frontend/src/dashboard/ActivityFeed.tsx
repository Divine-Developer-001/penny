/** Renders Penny's ledger events as an AG Grid: time plus the one-line explanation. */
import { useMemo } from "react";
import { AgGridReact } from "ag-grid-react";
import type { CellStyle, ColDef } from "ag-grid-community";
import { timeLabel } from "../format";
import { compactTheme } from "./gridTheme";
import type { ActivityItem } from "../types";

const TIME_STYLE: CellStyle = { color: "#6b7280", fontSize: "11.5px" };
const TEXT_STYLE: CellStyle = { lineHeight: "1.4", padding: "6px 8px" };

export default function ActivityFeed({ activity }: { activity: ActivityItem[] }) {
  const rows = useMemo(() => activity.map((a, i) => ({ id: `${a.created_at}-${i}`, time: a.created_at, text: a.description })), [activity]);
  type Row = (typeof rows)[number];
  const cols = useMemo<ColDef<Row>[]>(() => [
    { field: "time", headerName: "When", width: 118, valueFormatter: (p) => timeLabel(p.value as string), cellStyle: TIME_STYLE },
    { field: "text", headerName: "What Penny did", flex: 1, minWidth: 180, wrapText: true, autoHeight: true, cellStyle: TEXT_STYLE },
  ], []);
  if (rows.length === 0) return <div className="muted small">No activity yet.</div>;
  return (
    <div className="grid-wrap activity">
      <AgGridReact<Row> rowData={rows} columnDefs={cols} theme={compactTheme} domLayout="normal" suppressCellFocus getRowId={(p) => p.data.id}
        headerHeight={30} rowHeight={undefined} />
    </div>
  );
}
