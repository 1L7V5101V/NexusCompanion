// Public dashboard SDK surface shared with plugin panels. Plugins import this
// via the "@nexus/dashboard-ui" specifier (resolved through the host import
// map to a shim backed by window.__nexusRuntime), so they reuse the host's
// component implementations and its single React instance.
export * from "./ui";
export { cn } from "./cn";
export { Pie, MetricTile, Sparkline, BarChart } from "./charts";
export type { ChartTone } from "./charts";
export { api, asPageResult, pageCount } from "../api";
