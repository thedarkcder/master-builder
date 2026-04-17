"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { RefreshCw, SlidersHorizontal, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatTimestamp } from "@/lib/datetime";
import { listWorkflowTypes, type WorkflowTypeSummaryRecord } from "@/lib/api";
import { cn } from "@/lib/utils";

export default function TenantWorkflowTypesPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials, ready } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);

  const [workflowTypes, setWorkflowTypes] = useState<WorkflowTypeSummaryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [search, setSearch] = useState("");

  const loadWorkflowTypes = useCallback(async () => {
    if (!credentials) return;
    setLoading(true);
    try {
      const payload = await listWorkflowTypes(credentials, { tenantId });
      setWorkflowTypes(payload);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load workflows: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [credentials, tenantId]);

  useEffect(() => {
    if (ready && credentials) void loadWorkflowTypes();
  }, [ready, credentials, loadWorkflowTypes]);

  const filteredWorkflowTypes = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) {
      return workflowTypes;
    }
    return workflowTypes.filter((workflowType) => {
      return (
        workflowType.label.toLowerCase().includes(needle)
        || workflowType.key.toLowerCase().includes(needle)
        || String(workflowType.description || "").toLowerCase().includes(needle)
      );
    });
  }, [search, workflowTypes]);

  const hasFilter = search.trim().length > 0;

  return (
    <div className="space-y-4">
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-5">
          <div className="overflow-x-auto -mx-1 px-1">
            <div className="flex min-w-max flex-nowrap items-end gap-2 md:min-w-0 md:flex-wrap">
              <div className="flex shrink-0 items-center gap-1.5 text-muted-foreground">
                <SlidersHorizontal className="h-4 w-4" />
                <span className="text-[11px] font-medium uppercase tracking-[0.18em]">Filters</span>
              </div>

              <Input
                className="h-8 w-48 text-sm"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Workflow type"
              />

              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                {hasFilter ? (
                  <Button variant="ghost" size="sm" className="h-8 text-xs text-muted-foreground" onClick={() => setSearch("")}>
                    <X className="mr-1 h-3 w-3" />
                    Clear
                  </Button>
                ) : null}
                <Button variant="outline" size="sm" className="h-8" onClick={() => void loadWorkflowTypes()} disabled={loading}>
                  <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", loading && "animate-spin")} />
                  Refresh
                </Button>
              </div>
            </div>
          </div>
        </div>

        {statusLine ? (
          <div className="border-t px-5 py-3 text-sm text-muted-foreground">{statusLine}</div>
        ) : null}

        {filteredWorkflowTypes.length === 0 && !loading ? (
          <div className="border-t px-5 py-12 text-center text-sm text-muted-foreground">
            No workflows found for the current filters.
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Workflow</TableHead>
                <TableHead>Operations</TableHead>
                <TableHead>Executions</TableHead>
                <TableHead>Latest execution</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody className={cn(loading && "pointer-events-none opacity-50 transition-opacity")}>
              {filteredWorkflowTypes.map((workflowType) => (
                <TableRow key={workflowType.key}>
                  <TableCell className="font-medium">
                    <Link
                      className="text-primary hover:underline"
                      href={`/${encodeURIComponent(tenantId)}/workflows/${encodeURIComponent(workflowType.key)}`}
                    >
                      {workflowType.label}
                    </Link>
                    <p className="text-xs text-muted-foreground">{workflowType.key}</p>
                    {workflowType.description ? (
                      <p className="mt-1 max-w-[640px] text-sm text-muted-foreground">{workflowType.description}</p>
                    ) : null}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">{workflowType.operation_count}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">{workflowType.execution_count}</TableCell>
                  <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                    {workflowType.latest_execution_at ? formatTimestamp(workflowType.latest_execution_at) : "—"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </div>
  );
}
