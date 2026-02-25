"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Legend,
  Bar,
  BarChart,
  Cell,
  Scatter,
  ScatterChart,
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
} from "recharts";
import type { ReactElement } from "react";

type ChartSeries = {
  key: string;
  label: string;
  color?: string;
  stackId?: string;
};

type BaseChartProps<T extends Record<string, unknown>> = {
  data: T[];
  xAxisKey: keyof T & string;
  lines?: ChartSeries[];
  bars?: ChartSeries[];
  height?: number;
  className?: string;
};

function ensureValue(value: unknown): number | string {
  if (value === null || value === undefined || value === "") {
    return 0;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : 0;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

export function TokenLineChart<T extends Record<string, unknown>>(props: BaseChartProps<T>): ReactElement {
  const {
    data,
    xAxisKey,
    lines = [],
    height = 280,
    className = "",
  } = props;
  if (data.length === 0 || lines.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No chart data available for the selected filters. If this is unexpected, verify token usage rows are available for this tenant/project.
      </div>
    );
  }
  return (
    <div className={`w-full ${className}`}>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey={xAxisKey} />
          <YAxis />
          <Tooltip />
          <Legend />
          {lines.map((line) => (
            <Line
              key={line.key}
              type="monotone"
              dataKey={line.key}
              name={line.label}
              stroke={line.color ?? "#3b82f6"}
              dot={false}
              activeDot={{ r: 3 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function TokenStackedBarChart<T extends Record<string, unknown>>(props: BaseChartProps<T>): ReactElement {
  const {
    data,
    xAxisKey,
    bars = [],
    height = 280,
    className = "",
  } = props;
  if (data.length === 0 || bars.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No chart data available for the selected filters. If this is unexpected, verify token usage rows are available for this tenant/project.
      </div>
    );
  }
  return (
    <div className={`w-full ${className}`}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey={xAxisKey} />
          <YAxis />
          <Tooltip />
          <Legend />
          {bars.map((bar) => (
            <Bar
              key={bar.key}
              dataKey={bar.key}
              name={bar.label}
              fill={bar.color ?? "#3b82f6"}
              stackId={bar.stackId}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

type ScatterPoint = {
  x: number | string;
  y: number;
  label?: string;
};

type ScatterChartProps<T extends Record<string, unknown>> = {
  data: T[];
  xAxisKey: keyof T & string;
  yAxisKey: keyof T & string;
  height?: number;
  color?: string;
  pointLabel?: string;
};

type RadarSeries = {
  key: string;
  label: string;
  color?: string;
};

type RadarChartProps<T extends Record<string, unknown>> = {
  data: T[];
  axisKey: keyof T & string;
  series: RadarSeries[];
  height?: number;
};

export function TokenScatterChart<T extends Record<string, unknown>>(props: ScatterChartProps<T>): ReactElement {
  const {
    data,
    xAxisKey,
    yAxisKey,
    height = 280,
    color = "#0ea5e9",
    pointLabel = "point",
  } = props;
  if (data.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No chart data available for the selected filters. If this is unexpected, verify token usage rows are available for this tenant/project.
      </div>
    );
  }
  const scatterData = data.map((item) => ({
    x: ensureValue(item[xAxisKey]),
    y: Number(ensureValue(item[yAxisKey])),
    label: String(item[xAxisKey]),
  })) as ScatterPoint[];
  return (
    <div className="w-full">
      <ResponsiveContainer width="100%" height={height}>
        <ScatterChart>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis type="number" dataKey="x" name={String(xAxisKey)} />
          <YAxis type="number" dataKey="y" name={pointLabel} />
          <Tooltip />
          <Scatter name={pointLabel} data={scatterData} fill={color} />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}

export function TokenRadarChart<T extends Record<string, unknown>>(props: RadarChartProps<T>): ReactElement {
  const { data, axisKey, series, height = 320 } = props;
  if (data.length === 0 || series.length === 0) {
    return (
      <div className="text-xs text-muted-foreground">
        No chart data available for the selected filters. If this is unexpected, verify token usage rows are available for this tenant/project.
      </div>
    );
  }
  return (
    <div className="w-full">
      <ResponsiveContainer width="100%" height={height}>
        <RadarChart data={data}>
          <PolarGrid />
          <PolarAngleAxis dataKey={axisKey} />
          <PolarRadiusAxis />
          <Tooltip />
          <Legend />
          {series.map((item) => (
            <Radar
              key={item.key}
              dataKey={item.key}
              name={item.label}
              stroke={item.color ?? "#3b82f6"}
              fill={item.color ?? "#3b82f6"}
              fillOpacity={0.2}
            />
          ))}
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function formatMetricValue(value: number): string {
  if (!Number.isFinite(value)) {
    return "0";
  }
  if (Math.abs(value) >= 10000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}
