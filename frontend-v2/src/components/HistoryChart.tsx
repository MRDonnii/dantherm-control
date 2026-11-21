import { useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import { Table2, LineChart } from "lucide-react";
import { formatMoment, formatNumber, formatTick, nearestIndex, niceScale, timeTicks, typicalGap } from "../lib/chartScale";

type Sample = Record<string, number | null>;

/** Categorical slot (validated palette, see history.css). Slots are given in fixed order per chart. */
export type SeriesSlot = 1 | 2 | 3;
type LegacyColor = "blue" | "orange" | "green" | "red";

export interface HistorySeries {
  key: string;
  label: string;
  slot?: SeriesSlot;
  /** Older call sites; mapped onto the validated slots. */
  color?: LegacyColor;
  decimals?: number;
  /** Draw as steps (levels, setpoints) instead of a sloped line. */
  stepped?: boolean;
}

/** A value shown in the legend and table only (e.g. RPM next to %). */
export interface HistoryExtra { key: string; label: string; unit: string; decimals?: number }

export interface HistoryChartProps {
  samples: Sample[];
  series: HistorySeries[];
  unit?: string;
  decimals?: number;
  /** Total height in px, x-axis included. */
  height?: number;
  /** Smallest y-span, so a flat line is not blown up into noise. */
  minSpan?: number;
  /** Min/max spread per time bucket, washed behind one series. */
  band?: { seriesKey: string; minKey: string; maxKey: string; label: string };
  /** Stepped reference line from the samples (e.g. a setpoint). */
  reference?: { key: string; label: string; decimals?: number };
  /** A second stepped setpoint in its own colour (e.g. what the control sends HAC1 next to the target). */
  command?: { key: string; label: string; decimals?: number };
  /** Fixed y-axis instead of fitting the data (e.g. HAC1 levels 0..10). */
  yDomain?: { lo: number; hi: number; step: number };
  /** Fixed guide line (e.g. a recommended maximum). */
  threshold?: { value: number; label: string };
  /** 0..1 share per bucket, drawn as a strip under the plot (e.g. valve open). */
  strip?: { key: string; label: string };
  /**
   * A 0..100 % layer inside the plot's lower part with its own scale on the right edge:
   * the actual value as a stepped filled area (e.g. valve opening) and, optionally, a
   * decision as a dashed stepped line on the same scale. Where the decision is missing
   * but the value is known, the legend says decisionIdle instead.
   */
  valve?: ValveLayer;
  extras?: HistoryExtra[];
  /** Time window; defaults to the data extent. All charts on a page share it. */
  range?: { from: number; to: number };
  /** Controlled crosshair, so several charts can follow the same moment. */
  hoverTs?: number | null;
  onHover?: (ts: number | null) => void;
  loading?: boolean;
  title?: string;
}

export interface ValveLayer {
  key: string;
  label: string;
  decisionKey?: string;
  decisionLabel?: string;
  decisionIdle?: string;
  /** Share of the plot height the 0..100 % scale uses, from the bottom (default 0.4). */
  share?: number;
}

const LEGACY_SLOT: Record<LegacyColor, SeriesSlot> = { blue: 1, orange: 2, green: 3, red: 2 };
const slotOf = (item: HistorySeries): SeriesSlot => item.slot ?? (item.color ? LEGACY_SLOT[item.color] : 1);
const colorOf = (item: HistorySeries) => `var(--hx-${slotOf(item)})`;

const AXIS_BAND = 24;
const STRIP_BAND = 18;
const PAD_TOP = 10;
const PAD_RIGHT = 12;
/** Room for the valve layer's "100 %" labels on the right edge. */
const PAD_RIGHT_VALVE = 40;
const VALVE_TICKS = [0, 50, 100];

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fixedScale({ lo, hi, step }: { lo: number; hi: number; step: number }) {
  const ticks: number[] = [];
  for (let value = lo; value <= hi + step / 2; value += step) ticks.push(Math.round(value / step) * step);
  return { lo, hi, ticks };
}

function lastValue(samples: Sample[], key: string): { value: number; index: number } | null {
  for (let index = samples.length - 1; index >= 0; index -= 1) {
    const value = num(samples[index][key]);
    if (value !== null) return { value, index };
  }
  return null;
}

export function HistoryChart({
  samples, series, unit = "", decimals = 1, height = 200, minSpan = 0, band, reference, command, threshold, strip, valve,
  extras = [], range, hoverTs, onHover, loading = false, title, yDomain,
}: HistoryChartProps) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [localHover, setLocalHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const hover = hoverTs !== undefined ? hoverTs : localHover;
  const setHover = onHover ?? setLocalHover;

  useEffect(() => {
    const target = container.current;
    if (!target) return;
    const update = () => setWidth(Math.max(240, Math.round(target.getBoundingClientRect().width)));
    update();
    const observer = new ResizeObserver(update);
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  const rows = useMemo(() => samples.filter(sample => typeof sample.ts === "number").sort((a, b) => (a.ts as number) - (b.ts as number)), [samples]);
  const times = useMemo(() => rows.map(sample => sample.ts as number), [rows]);
  const gap = useMemo(() => typicalGap(times), [times]);

  const domainValues: number[] = [];
  for (const sample of rows) {
    for (const item of series) { const value = num(sample[item.key]); if (value !== null) domainValues.push(value); }
    if (band) for (const key of [band.minKey, band.maxKey]) { const value = num(sample[key]); if (value !== null) domainValues.push(value); }
    if (reference) { const value = num(sample[reference.key]); if (value !== null) domainValues.push(value); }
    // The command to HAC1 is not part of the scale: in Pi mode it runs several
    // degrees above the air and would squeeze the air and the target flat; it
    // is drawn clamped to the plot instead.
  }
  const hasData = domainValues.length > 0;
  // Optional layers only appear once the period holds data for them.
  const has = (key: string) => rows.some(sample => num(sample[key]) !== null);
  if (strip && !has(strip.key)) strip = undefined;
  if (reference && !has(reference.key)) reference = undefined;
  if (command && !has(command.key)) command = undefined;
  if (valve && !has(valve.key)) valve = undefined;
  if (valve) strip = undefined;
  if (band && !rows.some(sample => { const lo = num(sample[band!.minKey]); const hi = num(sample[band!.maxKey]); return lo !== null && hi !== null && hi - lo > 0.05; })) band = undefined;
  if (threshold && hasData) domainValues.push(threshold.value);

  const from = range?.from ?? times[0] ?? 0;
  const to = range?.to ?? times[times.length - 1] ?? 1;
  const span = Math.max(1, to - from);
  const scale = yDomain ? fixedScale(yDomain) : niceScale(Math.min(...domainValues), Math.max(...domainValues), minSpan);
  const tickDecimals = scale.ticks.some(tick => !Number.isInteger(tick)) ? 1 : 0;
  const longest = Math.max(...scale.ticks.map(tick => formatNumber(tick, tickDecimals).length));
  const left = 12 + longest * 7.2;
  const stripBand = strip ? STRIP_BAND : 0;
  const plotBottom = height - AXIS_BAND - stripBand;
  const padRight = valve ? PAD_RIGHT_VALVE : PAD_RIGHT;
  const plotWidth = Math.max(10, width - left - padRight);
  const plotHeight = Math.max(10, plotBottom - PAD_TOP);
  const xFor = (ts: number) => left + ((ts - from) / span) * plotWidth;
  const yFor = (value: number) => PAD_TOP + plotHeight - ((value - scale.lo) / (scale.hi - scale.lo)) * plotHeight;
  const breaks = (index: number) => index > 0 && times[index] - times[index - 1] > gap * 3;
  // The valve layer's own 0..100 % scale in the plot's lower part.
  const valveHeight = plotHeight * Math.min(1, Math.max(0.15, valve?.share ?? 0.4));
  const yValve = (percent: number) => plotBottom - (Math.min(100, Math.max(0, percent)) / 100) * valveHeight;

  const linePath = (key: string, stepped = false, y: (value: number) => number = yFor) => {
    let d = "";
    let previous: number | null = null;
    rows.forEach((sample, index) => {
      const value = num(sample[key]);
      if (value === null || breaks(index)) previous = null;
      if (value === null) return;
      const x = xFor(times[index]).toFixed(1);
      const yText = y(value).toFixed(1);
      if (previous === null) d += `M${x} ${yText}`;
      else d += stepped ? `H${x}V${yText}` : `L${x} ${yText}`;
      previous = value;
    });
    return d;
  };

  /** The valve opening as a stepped area down to the plot's bottom, one shape per run of data. */
  const valvePath = (key: string) => {
    let d = "";
    let open: { x: string } | null = null;
    const base = plotBottom.toFixed(1);
    rows.forEach((sample, index) => {
      const value = num(sample[key]);
      if (open && (value === null || breaks(index))) {
        const end = xFor(times[index - 1]).toFixed(1);
        d += `H${end}V${base}Z`;
        open = null;
      }
      if (value === null) return;
      const x = xFor(times[index]).toFixed(1);
      const y = yValve(value).toFixed(1);
      if (!open) { d += `M${x} ${base}V${y}`; open = { x }; }
      else d += `H${x}V${y}`;
    });
    if (open) d += `H${xFor(times[times.length - 1]).toFixed(1)}V${base}Z`;
    return d;
  };

  const bandPath = () => {
    if (!band) return "";
    const segments: { x: number; lo: number; hi: number }[][] = [];
    let current: { x: number; lo: number; hi: number }[] = [];
    rows.forEach((sample, index) => {
      const lo = num(sample[band.minKey]);
      const hi = num(sample[band.maxKey]);
      if (lo === null || hi === null || breaks(index)) { if (current.length) segments.push(current); current = []; }
      if (lo === null || hi === null) return;
      current.push({ x: xFor(times[index]), lo: yFor(lo), hi: yFor(hi) });
    });
    if (current.length) segments.push(current);
    return segments.filter(segment => segment.length > 1).map(segment =>
      `M${segment.map(point => `${point.x.toFixed(1)} ${point.hi.toFixed(1)}`).join("L")}` +
      `L${[...segment].reverse().map(point => `${point.x.toFixed(1)} ${point.lo.toFixed(1)}`).join("L")}Z`).join("");
  };

  const activeIndex = hover === null || rows.length === 0 ? -1 : nearestIndex(times, hover);
  const active = activeIndex >= 0 && Math.abs(times[activeIndex] - (hover as number)) <= Math.max(gap * 2, 90) ? activeIndex : -1;
  const valueAt = (key: string, latestOnly = false) => {
    if (active >= 0) return num(rows[active][key]);
    if (latestOnly) return rows.length ? num(rows[rows.length - 1][key]) : null;
    return lastValue(rows, key)?.value ?? null;
  };
  const show = (value: number | null, digits: number, suffix: string) => value === null ? "–" : `${formatNumber(value, digits)}${suffix}`;
  const showDecision = (decision: number | null, actual: number | null) =>
    decision !== null ? show(decision, 0, " %") : actual !== null && valve?.decisionIdle ? valve.decisionIdle : "–";

  const pointerToTs = (event: PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = (event.clientX - bounds.left) * (width / bounds.width);
    const ts = from + ((x - left) / plotWidth) * span;
    const index = nearestIndex(times, ts);
    return index >= 0 ? times[index] : null;
  };
  const onKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (times.length === 0) return;
    const current = active >= 0 ? active : times.length - 1;
    const step = event.shiftKey ? 10 : 1;
    let next: number | null = null;
    if (event.key === "ArrowLeft") next = Math.max(0, current - step);
    else if (event.key === "ArrowRight") next = Math.min(times.length - 1, current + step);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = times.length - 1;
    else if (event.key === "Escape") { setHover(null); return; }
    if (next === null) return;
    event.preventDefault();
    setHover(times[next]);
  };

  const legend = (
    <div className="hx-legend">
      <span className="hx-when">{active >= 0 ? formatMoment(times[active], span) : "Seneste"}</span>
      {series.map(item => (
        <span key={item.key} className="hx-item">
          <i className="hx-key" style={{ "--c": colorOf(item) } as CSSProperties}/>
          <span className="hx-label">{item.label}</span>
          <strong>{show(valueAt(item.key), item.decimals ?? decimals, unit)}</strong>
        </span>
      ))}
      {band && (() => {
        const lo = valueAt(band.minKey); const hi = valueAt(band.maxKey);
        const owner = series.find(item => item.key === band.seriesKey);
        return (
          <span className="hx-item">
            <i className="hx-key hx-key-band" style={{ "--c": owner ? colorOf(owner) : "var(--hx-1)" } as CSSProperties}/>
            <span className="hx-label">{band.label}</span>
            <strong>{lo === null || hi === null ? "–" : `${formatNumber(lo, decimals)}–${formatNumber(hi, decimals)}${unit}`}</strong>
          </span>
        );
      })()}
      {reference && (
        <span className="hx-item">
          <i className="hx-key hx-key-ref"/>
          <span className="hx-label">{reference.label}</span>
          <strong>{show(valueAt(reference.key), reference.decimals ?? 0, unit)}</strong>
        </span>
      )}
      {command && (
        <span className="hx-item">
          <i className="hx-key hx-key-cmd"/>
          <span className="hx-label">{command.label}</span>
          <strong>{show(valueAt(command.key), command.decimals ?? 0, unit)}</strong>
        </span>
      )}
      {valve && (
        <span className="hx-item">
          <i className="hx-key hx-key-valve"/>
          <span className="hx-label">{valve.label}</span>
          <strong>{show(valueAt(valve.key), 0, " %")}</strong>
        </span>
      )}
      {valve?.decisionKey && (
        <span className="hx-item">
          <i className="hx-key hx-key-decision"/>
          <span className="hx-label">{valve.decisionLabel ?? "Beslutning"}</span>
          <strong>{showDecision(valueAt(valve.decisionKey, true), valueAt(valve.key))}</strong>
        </span>
      )}
      {strip && (
        <span className="hx-item">
          <i className="hx-key hx-key-strip"/>
          <span className="hx-label">{strip.label}</span>
          <strong>{(() => { const share = valueAt(strip.key); return share === null ? "–" : `${Math.round(share * 100)} %`; })()}</strong>
        </span>
      )}
      {extras.map(extra => (
        <span key={extra.key} className="hx-item hx-extra">
          <span className="hx-label">{extra.label}</span>
          <strong>{show(valueAt(extra.key), extra.decimals ?? 0, extra.unit)}</strong>
        </span>
      ))}
      {hasData && (
        <button type="button" className="hx-view" aria-pressed={table} onClick={() => setTable(value => !value)}>
          {table ? <LineChart size={14}/> : <Table2 size={14}/>}<span>{table ? "Graf" : "Tabel"}</span>
        </button>
      )}
    </div>
  );

  if (!hasData) {
    return (
      <div ref={container} className={`hx-chart${loading ? " hx-loading" : ""}`}>
        <div className="history-chart-empty" style={{ height }}><span>{loading ? "Henter…" : "Ingen data i den valgte periode"}</span></div>
      </div>
    );
  }

  if (table) {
    const picks = Math.min(rows.length, 48);
    const indexes = Array.from({ length: picks }, (_, position) => Math.round((rows.length - 1) - position * ((rows.length - 1) / Math.max(1, picks - 1))));
    return (
      <div ref={container} className={`hx-chart${loading ? " hx-loading" : ""}`}>
        {legend}
        <div className="hx-table-wrap" style={{ maxHeight: Math.max(height, 220) }}>
          <table className="hx-table">
            <thead><tr><th>Tid</th>{series.map(item => <th key={item.key}>{item.label}</th>)}{band && <th>{band.label}</th>}{reference && <th>{reference.label}</th>}{command && <th>{command.label}</th>}{valve && <th>{valve.label}</th>}{valve?.decisionKey && <th>{valve.decisionLabel ?? "Beslutning"}</th>}{strip && <th>{strip.label}</th>}{extras.map(extra => <th key={extra.key}>{extra.label}</th>)}</tr></thead>
            <tbody>
              {indexes.map(index => {
                const sample = rows[index];
                return (
                  <tr key={times[index]}>
                    <td>{formatMoment(times[index], span)}</td>
                    {series.map(item => <td key={item.key}>{show(num(sample[item.key]), item.decimals ?? decimals, unit)}</td>)}
                    {band && <td>{num(sample[band.minKey]) === null ? "–" : `${formatNumber(num(sample[band.minKey]) as number, decimals)}–${formatNumber(num(sample[band.maxKey]) as number, decimals)}`}</td>}
                    {reference && <td>{show(num(sample[reference.key]), reference.decimals ?? 0, unit)}</td>}
                    {command && <td>{show(num(sample[command.key]), command.decimals ?? 0, unit)}</td>}
                    {valve && <td>{show(num(sample[valve.key]), 0, " %")}</td>}
                    {valve?.decisionKey && <td>{showDecision(num(sample[valve.decisionKey]), num(sample[valve.key]))}</td>}
                    {strip && <td>{num(sample[strip.key]) === null ? "–" : `${Math.round((num(sample[strip.key]) as number) * 100)} %`}</td>}
                    {extras.map(extra => <td key={extra.key}>{show(num(sample[extra.key]), extra.decimals ?? 0, extra.unit)}</td>)}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  const xTicks = timeTicks(from, to, plotWidth);
  const crossX = active >= 0 ? xFor(times[active]) : null;
  const momentLabel = active < 0 ? "" : span <= 2 * 86400 ? formatTick(times[active], 0) : `${formatTick(times[active], span)} ${formatTick(times[active], 0)}`;
  const momentWidth = momentLabel.length * 7 + 14;

  return (
    <div ref={container} className={`hx-chart${loading ? " hx-loading" : ""}`}>
      {legend}
      <svg
        width={width} height={height} viewBox={`0 0 ${width} ${height}`} tabIndex={0}
        role="img" aria-label={`${title ?? "Historikgraf"}: ${[...series.map(item => item.label), ...(valve ? [valve.label] : []), ...(valve?.decisionKey ? [valve.decisionLabel ?? "Beslutning"] : [])].join(", ")}. Brug piletasterne for at gå gennem målingerne.`}
        onPointerMove={event => setHover(pointerToTs(event))}
        onPointerDown={event => setHover(pointerToTs(event))}
        onPointerLeave={event => { if (event.pointerType === "mouse") setHover(null); }}
        onKeyDown={onKeyDown} onBlur={() => setHover(null)}
      >
        {scale.ticks.map(tick => (
          <g key={tick}>
            <line className="hx-grid" x1={left} x2={width - padRight} y1={yFor(tick)} y2={yFor(tick)}/>
            <text className="hx-tick" x={left - 8} y={yFor(tick) + 4} textAnchor="end">{formatNumber(tick, tickDecimals)}</text>
          </g>
        ))}
        {valve && (
          <g className="hx-valve">
            {VALVE_TICKS.map(tick => (
              <text key={tick} className="hx-tick hx-tick-valve" x={width - padRight + 5} y={yValve(tick) + (tick === 0 ? 0 : tick === 100 ? 7 : 3.5)} textAnchor="start">{`${tick} %`}</text>
            ))}
            <line className="hx-valve-top" x1={left} x2={width - padRight} y1={yValve(100)} y2={yValve(100)}/>
            <path className="hx-valve-area" d={valvePath(valve.key)}/>
            <path className="hx-valve-edge" d={linePath(valve.key, true, yValve)}/>
          </g>
        )}
        {threshold && threshold.value >= scale.lo && threshold.value <= scale.hi && (
          <g className="hx-threshold">
            <line x1={left} x2={width - padRight} y1={yFor(threshold.value)} y2={yFor(threshold.value)}/>
            <text x={width - padRight - 4} y={yFor(threshold.value) - 5} textAnchor="end">{threshold.label}</text>
          </g>
        )}
        {band && (() => {
          const owner = series.find(item => item.key === band.seriesKey);
          return <path className="hx-band" d={bandPath()} style={{ fill: owner ? colorOf(owner) : "var(--hx-1)" }}/>;
        })()}
        {valve?.decisionKey && <path className="hx-decision" d={linePath(valve.decisionKey, true, yValve)}/>}
        {command && <path className="hx-cmd" d={linePath(command.key, true, value => Math.min(Math.max(yFor(value), Math.min(yFor(scale.lo), yFor(scale.hi))), Math.max(yFor(scale.lo), yFor(scale.hi))))}/>}
        {reference && <path className="hx-ref" d={linePath(reference.key, true)}/>}
        {series.map(item => <path key={item.key} className="hx-line" d={linePath(item.key, item.stepped)} style={{ stroke: colorOf(item) }} stroke={colorOf(item)}/>)}
        {rows.length === 1 && series.map(item => {
          const value = num(rows[0][item.key]);
          return value === null ? null : <circle key={item.key} className="hx-dot" cx={xFor(times[0])} cy={yFor(value)} r={4} style={{ fill: colorOf(item) }}/>;
        })}
        {strip && (
          <g className="hx-strip">
            <rect className="hx-strip-track" x={left} y={plotBottom + 6} width={plotWidth} height={8} rx={2}/>
            {rows.map((sample, index) => {
              const share = num(sample[strip.key]);
              if (share === null || share <= 0) return null;
              const x0 = xFor(times[index]);
              const next = index + 1 < rows.length && !breaks(index + 1) ? times[index + 1] : times[index] + (Number.isFinite(gap) ? gap : 60);
              const x1 = Math.min(xFor(next), left + plotWidth);
              return <rect key={times[index]} x={x0} y={plotBottom + 6} width={Math.max(1, x1 - x0)} height={8} style={{ opacity: 0.2 + 0.8 * Math.min(1, share) }}/>;
            })}
          </g>
        )}
        {xTicks.map(tick => {
          const x = xFor(tick);
          if (crossX !== null && Math.abs(x - crossX) < momentWidth / 2 + 20) return null;
          const anchor = x < left + 24 ? "start" : x > width - padRight - 24 ? "end" : "middle";
          return <text key={tick} className="hx-tick" x={x} y={height - 7} textAnchor={anchor}>{formatTick(tick, span)}</text>;
        })}
        {crossX !== null && (
          <g className="hx-cross">
            <line x1={crossX} x2={crossX} y1={PAD_TOP} y2={plotBottom + stripBand}/>
            {series.map(item => {
              const value = num(rows[active][item.key]);
              return value === null ? null : <circle key={item.key} cx={crossX} cy={yFor(value)} r={4} style={{ fill: colorOf(item) }}/>;
            })}
            {valve && (() => {
              const value = num(rows[active][valve.key]);
              return value === null ? null : <circle className="hx-valve-dot" cx={crossX} cy={yValve(value)} r={3.5}/>;
            })()}
            <g transform={`translate(${Math.min(Math.max(crossX - momentWidth / 2, left), width - padRight - momentWidth)},${height - 21})`}>
              <rect width={momentWidth} height={18} rx={9}/>
              <text x={momentWidth / 2} y={13} textAnchor="middle">{momentLabel}</text>
            </g>
          </g>
        )}
      </svg>
    </div>
  );
}
