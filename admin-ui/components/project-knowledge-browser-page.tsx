"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { ProjectKnowledgeAddSection } from "@/components/project-knowledge-add-section";
import { ProjectKnowledgeSourcesSection } from "@/components/project-knowledge-sources-section";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast-provider";

import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  debugProjectKnowledgeSearch,
  deleteProjectKnowledgeAsset,
  getProjectKnowledgeAsset,
  getProjectKnowledgeStats,
  listProjectKnowledgeAssets,
  listProjectKnowledgeChunks,
  updateProjectKnowledgeAssetStatus,
  type ProjectKnowledgeAssetDetailRecord,
  type ProjectKnowledgeAssetRecord,
  type ProjectKnowledgeChunkRecord,
  type ProjectKnowledgeDebugMatchRecord,
  type ProjectKnowledgeStatsRecord
} from "@/lib/api";
import { canManageProjects } from "@/lib/auth-routing";
import { formatTimestamp } from "@/lib/datetime";

type ProjectKnowledgeBrowserPageProps = {
  tenantId: string;
  projectId: string;
  initialView?: "browse" | "add" | "sources";
};

const ASSET_PAGE_SIZE = 25;
const CHUNK_PAGE_SIZE = 10;

function summarizeSources(stats: ProjectKnowledgeStatsRecord | null): string {
  if (!stats) {
    return "No indexed sources yet.";
  }
  const entries = Object.entries(stats.source_type_counts ?? {});
  if (entries.length === 0) {
    return "No indexed sources yet.";
  }
  return entries
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([sourceType, count]) => `${sourceType}: ${count}`)
    .join(" · ");
}

export function ProjectKnowledgeBrowserPage({
  tenantId,
  projectId,
  initialView = "browse"
}: ProjectKnowledgeBrowserPageProps) {
  const { credentials, principal } = useAuth();
  const { showToast } = useToast();
  const [activeView, setActiveView] = useState<"browse" | "add" | "sources">(initialView);
  const [stats, setStats] = useState<ProjectKnowledgeStatsRecord | null>(null);
  const [assets, setAssets] = useState<ProjectKnowledgeAssetRecord[]>([]);
  const [assetTotal, setAssetTotal] = useState(0);
  const [assetOffset, setAssetOffset] = useState(0);
  const [statusFilter, setStatusFilter] = useState("all");
  const [sourceTypeFilter, setSourceTypeFilter] = useState("all");
  const [queryInput, setQueryInput] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [loadingPage, setLoadingPage] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedAsset, setSelectedAsset] = useState<ProjectKnowledgeAssetDetailRecord | null>(null);
  const [loadingSelectedAsset, setLoadingSelectedAsset] = useState(false);
  const [selectedChunks, setSelectedChunks] = useState<ProjectKnowledgeChunkRecord[]>([]);
  const [chunkTotal, setChunkTotal] = useState(0);
  const [chunkOffset, setChunkOffset] = useState(0);
  const [loadingChunks, setLoadingChunks] = useState(false);
  const [debugQuery, setDebugQuery] = useState("");
  const [debugMatches, setDebugMatches] = useState<ProjectKnowledgeDebugMatchRecord[]>([]);
  const [loadingDebug, setLoadingDebug] = useState(false);
  const [updatingAssetId, setUpdatingAssetId] = useState<string | null>(null);
  const [deletingAssetId, setDeletingAssetId] = useState<string | null>(null);

  const sourceTypeOptions = useMemo(() => {
    return Object.keys(stats?.source_type_counts ?? {}).sort((left, right) => left.localeCompare(right));
  }, [stats]);

  const actionsDisabled = !credentials || loadingPage;
  const allowProjectManagement = canManageProjects(principal, tenantId);

  const ensureCredentials = useCallback((): boolean => {
    if (credentials) {
      return true;
    }
    setStatusLine("Admin session is not ready yet. Wait a moment and try again.");
    return false;
  }, [credentials]);

  const loadPage = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setLoadingPage(true);
    try {
      const [statsPayload, page] = await Promise.all([
        getProjectKnowledgeStats(credentials, tenantId, projectId),
        listProjectKnowledgeAssets(credentials, tenantId, projectId, {
          limit: ASSET_PAGE_SIZE,
          offset: assetOffset,
          status: statusFilter === "all" ? undefined : statusFilter,
          sourceType: sourceTypeFilter === "all" ? undefined : sourceTypeFilter,
          query: appliedQuery || undefined
        })
      ]);
      setStats(statsPayload);
      setAssets(page.items);
      setAssetTotal(page.total);
    } catch (error) {
      setStatusLine(`Unable to load knowledge browser: ${(error as Error).message}`);
    } finally {
      setLoadingPage(false);
    }
  }, [credentials, tenantId, projectId, assetOffset, statusFilter, sourceTypeFilter, appliedQuery]);

  const loadSelectedAsset = useCallback(async () => {
    if (!credentials || !selectedAssetId) {
      return;
    }
    setLoadingSelectedAsset(true);
    try {
      const detail = await getProjectKnowledgeAsset(credentials, tenantId, projectId, selectedAssetId);
      setSelectedAsset(detail);
    } catch (error) {
      setStatusLine(`Unable to load knowledge asset: ${(error as Error).message}`);
      setSelectedAsset(null);
    } finally {
      setLoadingSelectedAsset(false);
    }
  }, [credentials, tenantId, projectId, selectedAssetId]);

  const loadSelectedChunks = useCallback(async () => {
    if (!credentials || !selectedAssetId) {
      return;
    }
    setLoadingChunks(true);
    try {
      const page = await listProjectKnowledgeChunks(credentials, tenantId, projectId, selectedAssetId, {
        limit: CHUNK_PAGE_SIZE,
        offset: chunkOffset
      });
      setSelectedChunks(page.items);
      setChunkTotal(page.total);
    } catch (error) {
      setStatusLine(`Unable to load knowledge chunks: ${(error as Error).message}`);
      setSelectedChunks([]);
      setChunkTotal(0);
    } finally {
      setLoadingChunks(false);
    }
  }, [credentials, tenantId, projectId, selectedAssetId, chunkOffset]);

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadPage();
  }, [credentials, loadPage]);

  useEffect(() => {
    if (!selectedAssetId || !credentials) {
      return;
    }
    void loadSelectedAsset();
  }, [credentials, loadSelectedAsset, selectedAssetId]);

  useEffect(() => {
    if (!selectedAssetId || !credentials) {
      return;
    }
    void loadSelectedChunks();
  }, [credentials, loadSelectedChunks, selectedAssetId, chunkOffset]);

  useEffect(() => {
    if (!allowProjectManagement && activeView !== "browse") {
      setActiveView("browse");
    }
  }, [activeView, allowProjectManagement]);

  function openAsset(assetId: string) {
    setSelectedAssetId(assetId);
    setSelectedAsset(null);
    setSelectedChunks([]);
    setChunkOffset(0);
  }

  function closeDrawer() {
    setSelectedAssetId(null);
    setSelectedAsset(null);
    setSelectedChunks([]);
    setChunkTotal(0);
    setChunkOffset(0);
  }

  async function refreshAll() {
    await loadPage();
    if (selectedAssetId) {
      await loadSelectedAsset();
      await loadSelectedChunks();
    }
  }

  async function runDebugSearch() {
    if (!ensureCredentials()) {
      return;
    }
    const activeCredentials = credentials;
    if (!activeCredentials) {
      return;
    }
    const trimmed = debugQuery.trim();
    if (!trimmed) {
      setDebugMatches([]);
      setStatusLine("Enter a query to inspect retrieval matches.");
      return;
    }
    setLoadingDebug(true);
    try {
      const payload = await debugProjectKnowledgeSearch(activeCredentials, tenantId, projectId, trimmed, 6);
      setDebugMatches(payload.items);
      setStatusLine(`Loaded ${payload.items.length} retrieval match${payload.items.length === 1 ? "" : "es"} for debug query.`);
    } catch (error) {
      setStatusLine(`Unable to debug retrieval: ${(error as Error).message}`);
      setDebugMatches([]);
    } finally {
      setLoadingDebug(false);
    }
  }

  async function removeAsset(assetId: string) {
    if (!ensureCredentials()) {
      return;
    }
    const activeCredentials = credentials;
    if (!activeCredentials) {
      return;
    }
    setDeletingAssetId(assetId);
    try {
      await deleteProjectKnowledgeAsset(activeCredentials, tenantId, projectId, assetId);
      if (selectedAssetId === assetId) {
        closeDrawer();
      }
      if (assets.length === 1 && assetOffset > 0) {
        setAssetOffset(Math.max(0, assetOffset - ASSET_PAGE_SIZE));
      }
      await refreshAll();
      showToast({ title: "Knowledge asset deleted", tone: "success" });
    } catch (error) {
      showToast({ title: "Knowledge asset delete failed", description: (error as Error).message, tone: "error" });
    } finally {
      setDeletingAssetId(null);
    }
  }

  async function updateAssetStatus(assetId: string, nextStatus: "pending_review" | "ready" | "rejected") {
    if (!ensureCredentials()) {
      return;
    }
    const activeCredentials = credentials;
    if (!activeCredentials) {
      return;
    }
    setUpdatingAssetId(assetId);
    try {
      const updatedAsset = await updateProjectKnowledgeAssetStatus(activeCredentials, tenantId, projectId, assetId, {
        status: nextStatus
      });
      if (selectedAssetId === assetId) {
        setSelectedAsset((current) => (current ? { ...current, status: updatedAsset.status, updated_at: updatedAsset.updated_at } : current));
      }
      await refreshAll();
      showToast({
        title:
          nextStatus === "ready"
            ? "Knowledge asset approved"
            : nextStatus === "rejected"
              ? "Knowledge asset rejected"
              : "Knowledge asset moved to review",
        description: updatedAsset.title,
        tone: "success",
      });
    } catch (error) {
      showToast({ title: "Knowledge asset update failed", description: (error as Error).message, tone: "error" });
    } finally {
      setUpdatingAssetId(null);
    }
  }

  const assetPageNumber = Math.floor(assetOffset / ASSET_PAGE_SIZE) + 1;
  const chunkPageNumber = Math.floor(chunkOffset / CHUNK_PAGE_SIZE) + 1;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2">
          <Button
            variant={activeView === "browse" ? "default" : "outline"}
            size="sm"
            onClick={() => {
              closeDrawer();
              setActiveView("browse");
            }}
          >
            Browse
          </Button>
          {allowProjectManagement ? (
            <>
              <Button
                variant={activeView === "add" ? "default" : "outline"}
                size="sm"
                onClick={() => {
                  closeDrawer();
                  setActiveView("add");
                }}
              >
                Add knowledge
              </Button>
              <Button
                variant={activeView === "sources" ? "default" : "outline"}
                size="sm"
                onClick={() => {
                  closeDrawer();
                  setActiveView("sources");
                }}
              >
                Sources
              </Button>
            </>
          ) : null}
        </div>
      </div>

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <h2 className="text-base font-semibold">Knowledge Analytics</h2>
        </div>
        <div className="space-y-3 p-6 pt-0">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Assets</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.total_assets ?? 0}</p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Ready</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.ready_assets ?? 0}</p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Pending Review</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.pending_review_assets ?? 0}</p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Rejected</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.rejected_assets ?? 0}</p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Chunks</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.total_chunks ?? 0}</p>
            </div>
            <div className="rounded-md border p-3">
              <p className="text-xs uppercase tracking-wide text-muted-foreground">Facts</p>
              <p className="mt-1 text-2xl font-semibold">{stats?.total_facts ?? 0}</p>
            </div>
          </div>
          <div className="rounded-md border p-3 text-sm text-muted-foreground">
            <p>
              <span className="font-medium text-foreground">Source mix:</span> {summarizeSources(stats)}
            </p>
            <p className="mt-1">
              <span className="font-medium text-foreground">Latest update:</span>{" "}
              {formatTimestamp(stats?.latest_asset_updated_at ?? null)}
            </p>
            <p className="mt-1">
              <span className="font-medium text-foreground">Fact states:</span>{" "}
              {`${stats?.approved_facts ?? 0} approved · ${stats?.pending_review_facts ?? 0} pending review · ${stats?.superseded_facts ?? 0} superseded`}
            </p>
          </div>
        </div>
      </div>

      {activeView === "add" ? (
        <ProjectKnowledgeAddSection
          credentials={credentials}
          tenantId={tenantId}
          projectId={projectId}
        />
      ) : null}

      {activeView === "sources" ? (
        <ProjectKnowledgeSourcesSection
          credentials={credentials}
          tenantId={tenantId}
          projectId={projectId}
        />
      ) : null}

      {activeView === "browse" ? (
      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="p-6 pb-3">
          <h2 className="text-base font-semibold">Asset Browser</h2>
        </div>
        <div className="space-y-4 p-6 pt-0">
          <div className="rounded-md border p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium">Retrieval Debug</p>
                <p className="text-sm text-muted-foreground">
                  Inspect which semantic facts or supporting chunks would answer a query.
                </p>
              </div>
              <div className="flex w-full gap-2 md:w-auto">
                <Input
                  value={debugQuery}
                  onChange={(event) => setDebugQuery(event.target.value)}
                  placeholder="Ask a knowledge question"
                  className="md:w-[28rem]"
                />
                <Button variant="outline" onClick={() => void runDebugSearch()} disabled={!credentials || loadingDebug}>
                  {loadingDebug ? "Running..." : "Debug query"}
                </Button>
              </div>
            </div>
            {debugMatches.length > 0 ? (
              <div className="mt-4 space-y-3">
                {debugMatches.map((match, index) => (
                  <div key={`${match.layer}-${match.fact_id ?? match.chunk_id ?? match.asset_id}-${index}`} className="rounded-md border p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>{match.layer}</span>
                      <span>score {match.score.toFixed(3)}</span>
                    </div>
                    <p className="mt-1 text-sm font-medium">{match.title}</p>
                    <p className="text-xs text-muted-foreground">
                      {match.source_type}
                      {match.source_ref ? ` · ${match.source_ref}` : ""}
                    </p>
                    <pre className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                      {match.snippet}
                    </pre>
                  </div>
                ))}
              </div>
            ) : null}
          </div>

          <div className="grid gap-3 md:grid-cols-4">
            <Input
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
              placeholder="Search title or source reference"
            />
            <select
              className="h-10 rounded-md border bg-background px-3 text-sm"
              value={statusFilter}
              onChange={(event) => {
                setAssetOffset(0);
                setStatusFilter(event.target.value);
              }}
            >
              <option value="all">All statuses</option>
              <option value="ready">Ready</option>
              <option value="pending_review">Pending review</option>
              <option value="rejected">Rejected</option>
            </select>
            <select
              className="h-10 rounded-md border bg-background px-3 text-sm"
              value={sourceTypeFilter}
              onChange={(event) => {
                setAssetOffset(0);
                setSourceTypeFilter(event.target.value);
              }}
            >
              <option value="all">All source types</option>
              {sourceTypeOptions.map((sourceType) => (
                <option key={sourceType} value={sourceType}>
                  {sourceType}
                </option>
              ))}
            </select>
            <div className="flex gap-2">
              <Button
                variant="outline"
                className="flex-1"
                onClick={() => {
                  setAssetOffset(0);
                  setAppliedQuery(queryInput.trim());
                }}
                disabled={!credentials}
              >
                Search
              </Button>
              <Button
                variant="ghost"
                className="flex-1"
                onClick={() => {
                  setQueryInput("");
                  setAppliedQuery("");
                  setStatusFilter("all");
                  setSourceTypeFilter("all");
                  setAssetOffset(0);
                }}
                disabled={!credentials}
              >
                Clear
              </Button>
            </div>
          </div>

          {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}

          {assets.length === 0 ? (
            <p className="rounded-md border p-4 text-sm text-muted-foreground">
              {loadingPage ? "Loading assets..." : "No matching knowledge assets."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Asset</TableHead>
                  <TableHead>Source</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Chunks</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {assets.map((asset) => (
                  <TableRow key={asset.asset_id}>
                    <TableCell>
                      <div className="space-y-1">
                        <p className="font-medium">{asset.title}</p>
                        <p className="text-xs text-muted-foreground">{asset.source_ref || "No source reference"}</p>
                      </div>
                    </TableCell>
                    <TableCell>{asset.source_type}</TableCell>
                    <TableCell>{asset.status}</TableCell>
                    <TableCell>{asset.chunk_count}</TableCell>
                    <TableCell>{formatTimestamp(asset.updated_at)}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex flex-wrap justify-end gap-2">
                        <Button size="sm" variant="outline" onClick={() => openAsset(asset.asset_id)} disabled={!credentials}>
                          View
                        </Button>
                        {allowProjectManagement && asset.status === "pending_review" ? (
                          <>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                              onClick={() => void updateAssetStatus(asset.asset_id, "ready")}
                            >
                              {updatingAssetId === asset.asset_id ? "Saving..." : "Approve"}
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                              onClick={() => void updateAssetStatus(asset.asset_id, "rejected")}
                            >
                              {updatingAssetId === asset.asset_id ? "Saving..." : "Reject"}
                            </Button>
                          </>
                        ) : null}
                        {allowProjectManagement && asset.status === "rejected" ? (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                            onClick={() => void updateAssetStatus(asset.asset_id, "pending_review")}
                          >
                            {updatingAssetId === asset.asset_id ? "Saving..." : "Move to review"}
                          </Button>
                        ) : null}
                        {allowProjectManagement ? (
                          <Button
                            size="sm"
                            variant="outline"
                            className="text-red-700 hover:text-red-800"
                            disabled={deletingAssetId === asset.asset_id}
                            onClick={() => void removeAsset(asset.asset_id)}
                          >
                            {deletingAssetId === asset.asset_id ? "Deleting..." : "Delete"}
                          </Button>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">
              Showing {assets.length === 0 ? 0 : assetOffset + 1}-{Math.min(assetOffset + assets.length, assetTotal)} of{" "}
              {assetTotal}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAssetOffset((current) => Math.max(0, current - ASSET_PAGE_SIZE))}
                disabled={loadingPage || assetOffset <= 0}
              >
                ← Prev
              </Button>
              <span className="text-sm text-muted-foreground">Page {assetPageNumber}</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAssetOffset((current) => current + ASSET_PAGE_SIZE)}
                disabled={loadingPage || assetOffset + ASSET_PAGE_SIZE >= assetTotal}
              >
                Next →
              </Button>
            </div>
          </div>
        </div>
      </div>
      ) : null}

      {activeView === "browse" && selectedAssetId ? (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={closeDrawer}>
          <div
            className="h-full w-full max-w-3xl overflow-y-auto bg-background p-6 shadow-xl"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-sm text-muted-foreground">Knowledge Asset</p>
                <h2 className="text-xl font-semibold">{selectedAsset?.title ?? "Loading asset..."}</h2>
                <p className="text-sm text-muted-foreground">{selectedAsset?.source_ref || "No source reference"}</p>
              </div>
              <Button variant="outline" size="sm" onClick={closeDrawer}>
                Close
              </Button>
            </div>

            {loadingSelectedAsset ? (
              <p className="mt-4 rounded-md border p-3 text-sm text-muted-foreground">Loading asset details...</p>
            ) : selectedAsset ? (
              <div className="mt-4 space-y-4">
                <div className="grid gap-3 md:grid-cols-2">
                  <div className="rounded-md border p-3 text-sm">
                    <p><span className="font-medium">Source type:</span> {selectedAsset.source_type}</p>
                    <p className="mt-1"><span className="font-medium">Status:</span> {selectedAsset.status}</p>
                    <p className="mt-1"><span className="font-medium">MIME type:</span> {selectedAsset.mime_type || "—"}</p>
                    <p className="mt-1"><span className="font-medium">Chunks:</span> {selectedAsset.chunk_count}</p>
                  </div>
                  <div className="rounded-md border p-3 text-sm">
                    <p><span className="font-medium">Created:</span> {formatTimestamp(selectedAsset.created_at)}</p>
                    <p className="mt-1"><span className="font-medium">Updated:</span> {formatTimestamp(selectedAsset.updated_at)}</p>
                    <p className="mt-1"><span className="font-medium">Source timestamp:</span> {formatTimestamp(selectedAsset.source_timestamp)}</p>
                  </div>
                </div>

                <div className="rounded-md border p-4">
                  <p className="mb-2 text-sm font-medium">Stored Content</p>
                  <pre className="whitespace-pre-wrap break-words text-sm text-muted-foreground">
                    {selectedAsset.text_content || "No extracted text content stored for this asset."}
                  </pre>
                </div>

                <div className="rounded-md border p-4">
                  <p className="mb-2 text-sm font-medium">Metadata</p>
                  <pre className="whitespace-pre-wrap break-words text-sm text-muted-foreground">
                    {JSON.stringify(selectedAsset.metadata_json ?? {}, null, 2)}
                  </pre>
                </div>

                <div className="rounded-md border p-4">
                  <p className="mb-2 text-sm font-medium">Extracted Facts</p>
                  <div className="space-y-3">
                    {selectedAsset.facts.length === 0 ? (
                      <p className="rounded-md border p-3 text-sm text-muted-foreground">
                        No normalized facts extracted for this asset.
                      </p>
                    ) : (
                      selectedAsset.facts.map((fact) => (
                        <div key={fact.fact_id} className="rounded-md border p-3">
                          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                            <span>{fact.fact_type}</span>
                            <span>
                              {fact.approval_state}
                              {fact.superseded_at ? ` · superseded ${formatTimestamp(fact.superseded_at)}` : ""}
                            </span>
                          </div>
                          <p className="mt-2 text-sm font-medium">{fact.fact_key}</p>
                          <p className="mt-1 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                            {fact.fact_value}
                          </p>
                          <div className="mt-2 text-xs text-muted-foreground">
                            <p>Confidence: {fact.confidence.toFixed(2)}</p>
                            <p>Source timestamp: {formatTimestamp(fact.source_timestamp)}</p>
                            {Object.keys(fact.metadata_json ?? {}).length > 0 ? (
                              <pre className="mt-2 whitespace-pre-wrap break-words">
                                {JSON.stringify(fact.metadata_json ?? {}, null, 2)}
                              </pre>
                            ) : null}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </div>

                <div className="rounded-md border p-4">
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-sm font-medium">Chunks</p>
                    <div className="flex items-center gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setChunkOffset((current) => Math.max(0, current - CHUNK_PAGE_SIZE))}
                        disabled={loadingChunks || chunkOffset <= 0}
                      >
                        ← Prev
                      </Button>
                      <span className="text-sm text-muted-foreground">Page {chunkPageNumber}</span>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setChunkOffset((current) => current + CHUNK_PAGE_SIZE)}
                        disabled={loadingChunks || chunkOffset + CHUNK_PAGE_SIZE >= chunkTotal}
                      >
                        Next →
                      </Button>
                    </div>
                  </div>
                  <div className="mt-3 space-y-3">
                    {loadingChunks ? (
                      <p className="rounded-md border p-3 text-sm text-muted-foreground">Loading chunks...</p>
                    ) : selectedChunks.length === 0 ? (
                      <p className="rounded-md border p-3 text-sm text-muted-foreground">No chunks stored for this asset.</p>
                    ) : (
                      selectedChunks.map((chunk) => (
                        <div key={chunk.chunk_id} className="rounded-md border p-3">
                          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
                            <span>Chunk {chunk.chunk_index + 1}</span>
                            <span>{chunk.token_count} tokens</span>
                          </div>
                          <pre className="mt-2 whitespace-pre-wrap break-words text-sm text-muted-foreground">
                            {chunk.content}
                          </pre>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              </div>
            ) : (
              <p className="mt-4 rounded-md border p-3 text-sm text-muted-foreground">Asset details unavailable.</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
