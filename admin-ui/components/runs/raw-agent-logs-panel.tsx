"use client";

import { Button } from "@/components/ui/button";
import type { RuntimeLogEventRecord } from "@/lib/api";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";

type RawAgentLogsPanelProps = {
  logs: RuntimeLogEventRecord[];
  filteredLogs: RuntimeLogEventRecord[];
  agentFilter: string;
  stageFilter: string;
  streamFilter: string;
  loadingOlderLogs: boolean;
  hasMoreLogs: boolean;
  stageColor: (stage: string) => string;
  onAgentFilterChange: (value: string) => void;
  onStageFilterChange: (value: string) => void;
  onStreamFilterChange: (value: string) => void;
  onLoadOlderLogs: () => void;
};

const LOG_FILTERS = {
  agent: [
    ["all", "All agents"],
    ["pm", "pm"],
    ["dev", "dev"],
    ["tester", "tester"],
    ["review", "review"],
  ],
  stage: [
    ["all", "All stages"],
    ["pm", "pm"],
    ["dev", "dev"],
    ["test", "test"],
    ["review", "review"],
    ["orchestrated_run", "orchestrated_run"],
  ],
  stream: [
    ["all", "All"],
    ["stdout", "stdout"],
    ["stderr", "stderr"],
  ],
} as const;

export function RawAgentLogsPanel({
  logs,
  filteredLogs,
  agentFilter,
  stageFilter,
  streamFilter,
  loadingOlderLogs,
  hasMoreLogs,
  stageColor,
  onAgentFilterChange,
  onStageFilterChange,
  onStreamFilterChange,
  onLoadOlderLogs,
}: RawAgentLogsPanelProps) {
  const filters = [
    { label: "Agent", value: agentFilter, onChange: onAgentFilterChange, options: LOG_FILTERS.agent },
    { label: "Stage", value: stageFilter, onChange: onStageFilterChange, options: LOG_FILTERS.stage },
    { label: "Stream", value: streamFilter, onChange: onStreamFilterChange, options: LOG_FILTERS.stream },
  ];

  return (
    <div className="px-5 py-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Raw Agent Logs</h2>
        <div className="flex items-center gap-1.5 text-xs">
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            onClick={onLoadOlderLogs}
            disabled={loadingOlderLogs || !hasMoreLogs || logs.length === 0}
          >
            {loadingOlderLogs ? "Loading..." : hasMoreLogs ? "Load older" : "All loaded"}
          </Button>
          {filters.map((filter) => (
            <select
              key={filter.label}
              className="h-7 rounded border border-input bg-background px-2 text-xs"
              value={filter.value}
              onChange={(event) => filter.onChange(event.target.value)}
            >
              {filter.options.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          ))}
        </div>
      </div>
      <div className="mt-3">
        {logs.length === 0 ? (
          <p className="text-xs text-muted-foreground">No logs captured yet.</p>
        ) : filteredLogs.length === 0 ? (
          <p className="text-xs text-muted-foreground">No log lines match current filters.</p>
        ) : (
          <ul className="max-h-[320px] space-y-2 overflow-y-auto pr-1 text-xs">
            {filteredLogs.map((entry, idx) => (
              <li
                key={`${entry.recorded_at}-${idx}`}
                className="rounded-lg border bg-background p-2.5"
                style={{ borderLeft: `2px solid ${stageColor(entry.stage)}` }}
              >
                <p className="mb-0.5 text-muted-foreground">
                  <span className="font-medium text-foreground">{entry.agent_id}</span> · {entry.stage}
                  {entry.attempt !== null ? ` #${entry.attempt}` : ""} [{entry.stream}] ·{" "}
                  <span title={formatTimestamp(entry.recorded_at)}>{formatTimeAgo(entry.recorded_at)}</span>
                </p>
                <p className="whitespace-pre-wrap">{entry.message}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
