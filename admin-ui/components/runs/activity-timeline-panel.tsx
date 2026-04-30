"use client";

import type { RefObject, ReactNode } from "react";
import { AlertCircle, Brain, ChevronRight, FileCode, MessageSquare, Terminal, Wrench } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatTimeAgo, formatTimestamp } from "@/lib/datetime";

export type ChatTimelineEntry = {
  key: string;
  recordedAt: string;
  stage: string;
  attempt: number | null;
  speaker: string;
  text: string;
  kind: "message" | "reasoning" | "status" | "error" | "command" | "file_edit" | "tool_call";
  meta?: {
    command?: string;
    exitCode?: number;
    output?: string;
    filePath?: string;
    language?: string;
  };
};

type ActivityTimelinePanelProps = {
  entries: ChatTimelineEntry[];
  visibleEntries: ChatTimelineEntry[];
  hasOlderMessages: boolean;
  autoScroll: boolean;
  listRef: RefObject<HTMLUListElement>;
  stageDisplayLabel: (stage: string) => string;
  onLoadOlder: () => void;
  onShowLatest: () => void;
};

const MAX_OUTPUT_PREVIEW = 600;

const TIMELINE_ICON: Record<ChatTimelineEntry["kind"], { icon: ReactNode; color: string }> = {
  message: { icon: <MessageSquare className="h-3.5 w-3.5" />, color: "text-blue-500" },
  reasoning: { icon: <Brain className="h-3.5 w-3.5" />, color: "text-violet-500" },
  status: { icon: <ChevronRight className="h-3.5 w-3.5" />, color: "text-muted-foreground" },
  error: { icon: <AlertCircle className="h-3.5 w-3.5" />, color: "text-red-500" },
  command: { icon: <Terminal className="h-3.5 w-3.5" />, color: "text-amber-500" },
  file_edit: { icon: <FileCode className="h-3.5 w-3.5" />, color: "text-emerald-500" },
  tool_call: { icon: <Wrench className="h-3.5 w-3.5" />, color: "text-orange-500" },
};

function TimelineRow({
  entry,
  stageDisplayLabel,
  isLast,
}: {
  entry: ChatTimelineEntry;
  stageDisplayLabel: (stage: string) => string;
  isLast: boolean;
}) {
  const { icon, color } = TIMELINE_ICON[entry.kind] ?? TIMELINE_ICON.message;
  const stageLabel = stageDisplayLabel(entry.stage);
  const ts = formatTimeAgo(entry.recordedAt);
  const fullTs = formatTimestamp(entry.recordedAt);
  const attempt = entry.attempt !== null ? ` #${entry.attempt}` : "";

  return (
    <li className="group relative flex gap-3 pb-4 last:pb-0">
      {!isLast ? <div className="absolute bottom-0 left-[13px] top-6 w-px bg-border" /> : null}

      <div className={`relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border bg-background ${color}`}>
        {icon}
      </div>

      <div className="min-w-0 flex-1 pt-0.5">
        <div className="flex items-baseline gap-1.5 text-xs">
          <span className="font-medium text-foreground">
            {entry.kind === "status"
              ? entry.text
              : entry.kind === "command"
                ? "Ran command"
                : entry.kind === "file_edit"
                  ? "Edited file"
                  : entry.kind === "tool_call"
                    ? `Called ${entry.text}`
                    : entry.kind === "reasoning"
                      ? "Thinking"
                      : entry.kind === "error"
                        ? "Error"
                        : stageLabel}
          </span>
          <span className="text-[10px] text-muted-foreground">
            {stageLabel}
            {attempt}
          </span>
          <span className="ml-auto shrink-0 text-[10px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" title={fullTs}>
            {ts}
          </span>
        </div>

        {entry.kind === "status" ? null : entry.kind === "reasoning" ? (
          <details className="mt-1 text-xs text-muted-foreground">
            <summary className="cursor-pointer select-none hover:text-foreground">Show reasoning</summary>
            <p className="mt-1.5 whitespace-pre-wrap italic leading-relaxed">{entry.text}</p>
          </details>
        ) : entry.kind === "command" ? (
          <div className="mt-1">
            <div className="inline-flex items-center gap-1.5 rounded-md bg-zinc-950 px-2.5 py-1 text-[11px] text-zinc-200">
              <code>{entry.meta?.command ?? entry.text}</code>
              {entry.meta?.exitCode != null ? (
                <span className={`ml-1 rounded px-1 py-px text-[9px] font-medium ${entry.meta.exitCode === 0 ? "bg-emerald-900/50 text-emerald-300" : "bg-red-900/50 text-red-300"}`}>
                  {entry.meta.exitCode}
                </span>
              ) : null}
            </div>
            {entry.meta?.output ? (
              <details className="mt-1.5 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none hover:text-foreground">Output</summary>
                <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted/50 px-2.5 py-2 text-[10px] leading-relaxed">
                  {entry.meta.output.slice(0, MAX_OUTPUT_PREVIEW)}
                  {entry.meta.output.length > MAX_OUTPUT_PREVIEW ? "\n..." : ""}
                </pre>
              </details>
            ) : null}
          </div>
        ) : entry.kind === "file_edit" ? (
          <div className="mt-1">
            <code className="text-xs font-medium">{entry.meta?.filePath || "file"}</code>
            {entry.text ? <span className="ml-1.5 text-xs text-muted-foreground">{entry.text}</span> : null}
            {entry.meta?.output ? (
              <details className="mt-1.5 text-xs text-muted-foreground">
                <summary className="cursor-pointer select-none hover:text-foreground">Diff</summary>
                <pre className="mt-1 max-h-40 overflow-auto rounded-md bg-muted/50 px-2.5 py-2 text-[10px] leading-relaxed">
                  {entry.meta.output.slice(0, MAX_OUTPUT_PREVIEW)}
                  {entry.meta.output.length > MAX_OUTPUT_PREVIEW ? "\n..." : ""}
                </pre>
              </details>
            ) : null}
          </div>
        ) : entry.kind === "tool_call" ? (
          <div className="mt-1 text-xs">
            {entry.meta?.output ? (
              <span className="text-muted-foreground">
                {entry.meta.output.slice(0, 160)}
                {entry.meta.output.length > 160 ? "..." : ""}
              </span>
            ) : null}
          </div>
        ) : entry.kind === "error" ? (
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed text-destructive">{entry.text}</p>
        ) : (
          <p className="mt-1 whitespace-pre-wrap text-xs leading-relaxed">{entry.text}</p>
        )}
      </div>
    </li>
  );
}

export function ActivityTimelinePanel({
  entries,
  visibleEntries,
  hasOlderMessages,
  autoScroll,
  listRef,
  stageDisplayLabel,
  onLoadOlder,
  onShowLatest,
}: ActivityTimelinePanelProps) {
  return (
    <div className="flex flex-col overflow-hidden rounded-2xl border bg-background">
      <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-background px-5 py-3">
        <h2 className="text-sm font-semibold">Activity Timeline</h2>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-muted-foreground">{entries.length} messages</span>
          {hasOlderMessages ? (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onLoadOlder}>
              Load older
            </Button>
          ) : null}
          {!autoScroll ? (
            <Button variant="outline" size="sm" className="h-7 text-xs" onClick={onShowLatest}>
              Latest
            </Button>
          ) : null}
        </div>
      </div>
      <div className="flex-1 px-5 py-4">
        {entries.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">No messages captured yet.</p>
        ) : (
          <ul ref={listRef} className="max-h-[560px] overflow-y-auto pr-1 text-xs">
            {visibleEntries.map((entry, idx) => (
              <TimelineRow
                key={entry.key}
                entry={entry}
                stageDisplayLabel={stageDisplayLabel}
                isLast={idx === visibleEntries.length - 1}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
