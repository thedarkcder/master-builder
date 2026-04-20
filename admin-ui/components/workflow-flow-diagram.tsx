"use client";

import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";

export type WorkflowFlowNode = {
  key: string;
  label: string;
  status?: string | null;
  detail?: string | null;
  impact?: string | null;
  completionRequired?: boolean | null;
};

type WorkflowFlowGroup = {
  main: WorkflowFlowNode;
  supporting: WorkflowFlowNode[];
};

function nodeTone(status?: string | null): string {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "failed") return "border-red-300 bg-red-50";
  if (normalized === "completed") return "border-emerald-300 bg-emerald-50";
  if (normalized === "waiting_for_input") return "border-amber-300 bg-amber-50";
  if (normalized === "running" || normalized === "retrying") return "border-blue-300 bg-blue-50";
  return "border-border bg-background";
}

function groupNodes(nodes: WorkflowFlowNode[]): WorkflowFlowGroup[] {
  const groups: WorkflowFlowGroup[] = [];
  for (const node of nodes) {
    if (node.completionRequired === false && groups.length > 0) {
      groups[groups.length - 1].supporting.push(node);
      continue;
    }
    groups.push({ main: node, supporting: [] });
  }
  return groups;
}

function FlowCard({ node }: { node: WorkflowFlowNode }) {
  return (
    <div className={cn("rounded-2xl border p-4 shadow-sm", nodeTone(node.status))}>
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
  );
}

export function WorkflowFlowDiagram({
  nodes,
  emptyLabel,
  orientation = "horizontal",
}: {
  nodes: WorkflowFlowNode[];
  emptyLabel: string;
  orientation?: "horizontal" | "vertical";
}) {
  if (nodes.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyLabel}</p>;
  }

  const groups = groupNodes(nodes);

  if (orientation === "vertical") {
    return (
      <div className="px-1 py-1">
        <div className="flex flex-col gap-3">
          {groups.map((group, index) => (
            <div key={group.main.key} className="flex flex-col gap-2">
              <div className="flex items-start gap-4">
                <div className="min-w-0 flex-1">
                  <FlowCard node={group.main} />
                </div>
                {group.supporting.length ? (
                  <div className="w-72 space-y-2 pt-3">
                    {group.supporting.map((node) => (
                      <div key={node.key} className="flex items-center gap-2">
                        <div className="h-px w-3 bg-border" />
                        <div className="h-1.5 w-1.5 rounded-full bg-border" />
                        <div className="min-w-0 flex-1">
                          <FlowCard node={node} />
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
              {index < groups.length - 1 ? (
                <div className="flex pl-8">
                  <div className="flex flex-col items-center gap-1 py-0.5">
                    <div className="h-3 w-px bg-border" />
                    <div className="h-1.5 w-1.5 rounded-full bg-border" />
                    <div className="h-3 w-px bg-border" />
                  </div>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto px-1 py-1">
      <div className="flex min-w-max items-start gap-3 pb-2">
        {groups.map((group, index) => (
          <div key={group.main.key} className="flex items-start gap-3">
            <div className="flex w-64 flex-col gap-2">
              <FlowCard node={group.main} />
              {group.supporting.length ? (
                <div className="space-y-2 pl-4">
                  {group.supporting.map((node) => (
                    <div key={node.key} className="flex items-center gap-2">
                      <div className="h-px w-3 bg-border" />
                      <div className="h-1.5 w-1.5 rounded-full bg-border" />
                      <div className="min-w-0 flex-1">
                        <FlowCard node={node} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
            {index < groups.length - 1 ? (
              <div className="flex h-10 items-center">
                <div className="flex items-center gap-1">
                  <div className="h-px w-4 bg-border" />
                  <div className="h-1.5 w-1.5 rounded-full bg-border" />
                  <div className="h-px w-4 bg-border" />
                </div>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}
