"use client";

import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";

export type WorkflowFlowNode = {
  key: string;
  label: string;
  status?: string | null;
  detail?: string | null;
  impact?: string | null;
};

function nodeTone(status?: string | null): string {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "failed") return "border-red-300 bg-red-50";
  if (normalized === "completed") return "border-emerald-300 bg-emerald-50";
  if (normalized === "waiting_for_input") return "border-amber-300 bg-amber-50";
  if (normalized === "running" || normalized === "retrying") return "border-blue-300 bg-blue-50";
  return "border-border bg-background";
}

export function WorkflowFlowDiagram({
  nodes,
  emptyLabel,
}: {
  nodes: WorkflowFlowNode[];
  emptyLabel: string;
}) {
  if (nodes.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyLabel}</p>;
  }

  return (
    <div className="overflow-x-auto px-1 py-1">
      <div className="flex min-w-max items-start gap-4 pb-2">
        {nodes.map((node, index) => (
          <div key={node.key} className="flex items-center gap-4">
            <div className={cn("w-64 rounded-2xl border p-4 shadow-sm", nodeTone(node.status))}>
              <div className="flex items-start justify-between gap-3">
                <div className="space-y-1">
                  <p className="text-sm font-semibold">{node.label}</p>
                  {node.impact ? (
                    <div className="inline-flex rounded-full border px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                      {node.impact}
                    </div>
                  ) : null}
                </div>
                {node.status ? <StatusBadge status={node.status} /> : null}
              </div>
              {node.detail ? <p className="mt-3 text-xs text-muted-foreground">{node.detail}</p> : null}
            </div>
            {index < nodes.length - 1 ? (
              <div className="flex h-16 items-center">
                <div className="flex items-center gap-0">
                  <div className="h-px w-8 bg-border" />
                  <div className="rounded-full border bg-background px-2 py-0.5 text-xs text-muted-foreground">→</div>
                  <div className="h-px w-8 bg-border" />
                </div>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}
