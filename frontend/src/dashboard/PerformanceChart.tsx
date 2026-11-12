/** Renders the performance chart (AG Charts line): portfolio value vs money put in over time.
 *  The value line is green while at/above cost and red below it; tooltips show date, value and P/L. */
import { useEffect, useMemo, useState } from "react";
import { AgCharts } from "ag-charts-react";
import type { AgCartesianChartOptions } from "ag-charts-community";
import { api } from "../api";
import { money, parseUtc, signedMoney } from "../format";
import type { HistoryPoint, HistoryRange } from "../types";

const RANGES: HistoryRange[] = ["7d", "30d", "all"];
const POLL_MS = 15000;

interface Row {
  t: Date;
  value: number;
  cost: number;
  above: number | null;
  below: number | null;
}

export function toRows(points: HistoryPoint[]): Row[] {
  const base = points.map((p) => ({ t: parseUtc(p.t), value: Number(p.value), cost: Number(p.invested_cost) }));
  const up = base.map((r) => r.value >= r.cost);
  // Each colour also owns the neighbouring points at a crossing, so the two lines join without a gap.
  return base.map((r, i) => ({
    ...r,
    above: up[i] || up[i - 1] || up[i + 1] ? r.value : null,
    below: !up[i] || up[i - 1] === false || up[i + 1] === false ? r.value : null,
  }));
}

export default function PerformanceChart({ refreshKey }: { refreshKey: number }) {
  const [range, setRange] = useState<HistoryRange>("30d");
  const [points, setPoints] = useState<HistoryPoint[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => { api.getHistory(range).then((p) => { if (alive) setPoints(p); }).catch(() => undefined); };
    load();
    const id = window.setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
    return () => { alive = false; window.clearInterval(id); };
  }, [range, refreshKey]);

  const options = useMemo<AgCartesianChartOptions>(() => {
    const data = toRows(points ?? []);
    const tooltip = {
      renderer: ({ datum }: { datum: Row }) => ({
        heading: datum.t.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }),
        data: [
          { label: "Value", value: money(datum.value) },
          { label: "Put in", value: money(datum.cost) },
          { label: "P/L", value: signedMoney(datum.value - datum.cost) },
        ],
      }),
    };
    return {
      data,
      height: 210,
      padding: { top: 6, right: 8, bottom: 0, left: 0 },
      legend: { enabled: false },
      series: [
        { type: "line", xKey: "t", yKey: "cost", yName: "Money put in", stroke: "#8a8f98", lineDash: [5, 4], strokeWidth: 1.5, marker: { enabled: false } },
        { type: "line", xKey: "t", yKey: "above", yName: "Value (above cost)", stroke: "#0f9d58", strokeWidth: 2.2, marker: { enabled: false }, tooltip },
        { type: "line", xKey: "t", yKey: "below", yName: "Value (below cost)", stroke: "#d93025", strokeWidth: 2.2, marker: { enabled: false }, tooltip },
      ],
      axes: {
        x: { type: "time", label: { fontSize: 10 } },
        y: { type: "number", label: { fontSize: 10, formatter: ({ value }: { value: number }) => `$${value.toFixed(0)}` } },
      },
    } as AgCartesianChartOptions;
  }, [points]);

  return (
    <div className="sec">
      <div className="tabs">
        <h4 style={{ margin: 0 }}>Performance</h4><span className="sp" />
        {RANGES.map((r) => <button key={r} className={`tab${r === range ? " on" : ""}`} onClick={() => setRange(r)}>{r}</button>)}
      </div>
      {points === null ? <div className="muted small">Loading chart…</div>
        : points.length < 2 ? <div className="muted small">The chart starts once Penny has money to track.</div>
        : <>
            <AgCharts options={options} />
            <div className="legend">
              <span><i className="sw green" />Value above cost</span><span><i className="sw red" />Value below cost</span><span><i className="sw dash" />Money put in</span>
            </div>
          </>}
    </div>
  );
}
