"use client";

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createProjectKnowledgeAsset,
  deleteProjectKnowledgeAsset,
  getKnowledgeJiraSyncRuntimeStatus,
  listProjectKnowledgeAssets,
  syncProjectKnowledgeFromJira,
  updateProjectKnowledgeAssetStatus,
  type Credentials,
  type KnowledgeJiraSyncProjectStatusRecord,
  type ProjectKnowledgeAssetRecord,
  type ProjectKnowledgeSyncResult
} from "@/lib/api";

type ProjectKnowledgeBaseSectionProps = {
  credentials: Credentials | null;
  tenantId: string;
  projectId: string;
};

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

async function readFileAsBase64(file: File): Promise<string> {
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result !== "string") {
        reject(new Error(`Unable to encode ${file.name}`));
        return;
      }
      const delimiter = reader.result.indexOf(",");
      if (delimiter < 0) {
        reject(new Error(`Invalid file encoding for ${file.name}`));
        return;
      }
      resolve(reader.result.slice(delimiter + 1));
    };
    reader.onerror = () => {
      reject(reader.error ?? new Error(`Unable to read ${file.name}`));
    };
    reader.readAsDataURL(file);
  });
}

export function ProjectKnowledgeBaseSection({
  credentials,
  tenantId,
  projectId
}: ProjectKnowledgeBaseSectionProps) {
  const [assets, setAssets] = useState<ProjectKnowledgeAssetRecord[]>([]);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [uploadingFiles, setUploadingFiles] = useState(false);
  const [deletingAssetId, setDeletingAssetId] = useState<string | null>(null);
  const [updatingAssetId, setUpdatingAssetId] = useState<string | null>(null);
  const [syncingJira, setSyncingJira] = useState(false);
  const [lastSyncResult, setLastSyncResult] = useState<ProjectKnowledgeSyncResult | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<KnowledgeJiraSyncProjectStatusRecord | null>(null);
  const [draggingOver, setDraggingOver] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const totalChunks = assets.reduce((sum, asset) => sum + asset.chunk_count, 0);
  const readyAssets = assets.filter((asset) => asset.status === "ready").length;
  const pendingReviewAssets = assets.filter((asset) => asset.status === "pending_review").length;
  const rejectedAssets = assets.filter((asset) => asset.status === "rejected").length;
  const sourceTypeCounts = assets.reduce<Record<string, number>>((counts, asset) => {
    const sourceType = asset.source_type || "unknown";
    counts[sourceType] = (counts[sourceType] ?? 0) + 1;
    return counts;
  }, {});
  const sourceSummary = Object.entries(sourceTypeCounts)
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .map(([sourceType, count]) => `${sourceType}: ${count}`)
    .join(" · ");
  const latestAssetTimestamp = assets.reduce<string | null>((latest, asset) => {
    if (!asset.updated_at) {
      return latest;
    }
    if (!latest) {
      return asset.updated_at;
    }
    return new Date(asset.updated_at).getTime() > new Date(latest).getTime() ? asset.updated_at : latest;
  }, null);

  const loadAssets = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setLoadingAssets(true);
    try {
      const [payload, runtime] = await Promise.all([
        listProjectKnowledgeAssets(credentials, tenantId, projectId),
        getKnowledgeJiraSyncRuntimeStatus(credentials)
      ]);
      setAssets(payload);
      setRuntimeStatus(
        runtime.projects.find(
          (projectStatus) =>
            projectStatus.tenant_id === tenantId && projectStatus.project_id === projectId
        ) ?? null
      );
    } catch (error) {
      setStatusLine(`Unable to load knowledge assets: ${(error as Error).message}`);
    } finally {
      setLoadingAssets(false);
    }
  }, [credentials, tenantId, projectId]);

  useEffect(() => {
    if (!credentials) {
      return;
    }
    void loadAssets();
  }, [credentials, loadAssets]);

  async function uploadFiles(fileList: FileList | null) {
    if (!credentials) {
      return;
    }
    if (!fileList || fileList.length === 0) {
      return;
    }
    const files = Array.from(fileList);
    setUploadingFiles(true);
    setStatusLine(`Uploading ${files.length} file${files.length === 1 ? "" : "s"}...`);
    let uploaded = 0;
    let failed = 0;
    try {
      for (const file of files) {
        try {
          const contentBase64 = await readFileAsBase64(file);
          await createProjectKnowledgeAsset(credentials, tenantId, projectId, {
            title: file.name,
            source_type: "file_upload",
            mime_type: file.type || "application/octet-stream",
            source_ref: file.name,
            source_timestamp: new Date(file.lastModified).toISOString(),
            content_base64: contentBase64
          });
          uploaded += 1;
        } catch {
          failed += 1;
        }
      }
      await loadAssets();
      if (failed === 0) {
        setStatusLine(`Uploaded ${uploaded} file${uploaded === 1 ? "" : "s"}.`);
      } else {
        setStatusLine(`Uploaded ${uploaded} file${uploaded === 1 ? "" : "s"}, failed ${failed}.`);
      }
    } catch (error) {
      setStatusLine(`Unable to upload files: ${(error as Error).message}`);
    } finally {
      setUploadingFiles(false);
      setDraggingOver(false);
    }
  }

  async function removeAsset(assetId: string) {
    if (!credentials) {
      return;
    }
    setDeletingAssetId(assetId);
    try {
      await deleteProjectKnowledgeAsset(credentials, tenantId, projectId, assetId);
      setStatusLine("Knowledge asset deleted.");
      await loadAssets();
    } catch (error) {
      setStatusLine(`Unable to delete knowledge asset: ${(error as Error).message}`);
    } finally {
      setDeletingAssetId(null);
    }
  }

  async function updateAssetStatus(assetId: string, nextStatus: "pending_review" | "ready" | "rejected") {
    if (!credentials) {
      return;
    }
    setUpdatingAssetId(assetId);
    try {
      const updatedAsset = await updateProjectKnowledgeAssetStatus(credentials, tenantId, projectId, assetId, {
        status: nextStatus
      });
      setStatusLine(
        nextStatus === "ready"
          ? `Knowledge asset "${updatedAsset.title}" approved.`
          : nextStatus === "rejected"
            ? `Knowledge asset "${updatedAsset.title}" rejected.`
            : `Knowledge asset "${updatedAsset.title}" moved back to review.`
      );
      await loadAssets();
    } catch (error) {
      setStatusLine(`Unable to update knowledge asset status: ${(error as Error).message}`);
    } finally {
      setUpdatingAssetId(null);
    }
  }

  async function runJiraSync() {
    if (!credentials) {
      return;
    }
    setSyncingJira(true);
    setStatusLine("Syncing Jira knowledge...");
    try {
      const result = await syncProjectKnowledgeFromJira(credentials, tenantId, projectId);
      setLastSyncResult(result);
      setStatusLine(
        [
          `Jira sync complete.`,
          `Created ${result.created_assets}`,
          `updated ${result.updated_assets}`,
          `unchanged ${result.unchanged_assets}`,
          `deleted ${result.deleted_assets}`,
          `failed ${result.failed_assets}.`
        ].join(" ")
      );
      await loadAssets();
    } catch (error) {
      setStatusLine(`Unable to sync Jira knowledge: ${(error as Error).message}`);
    } finally {
      setSyncingJira(false);
    }
  }

  function onFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    void uploadFiles(event.target.files);
    event.target.value = "";
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (uploadingFiles) {
      return;
    }
    setDraggingOver(false);
    void uploadFiles(event.dataTransfer.files);
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>Knowledge Base</CardTitle>
            <CardDescription>
              Drop files to upload, review knowledge state, or sync Jira into the project
              knowledge base.
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => void runJiraSync()}
              disabled={syncingJira || loadingAssets}
            >
              {syncingJira ? "Syncing Jira..." : "Sync Jira now"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void loadAssets()}
              disabled={loadingAssets || syncingJira}
            >
              {loadingAssets ? "Refreshing..." : "Refresh"}
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
          <div className="rounded-md border p-3">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Assets</p>
            <p className="mt-1 text-2xl font-semibold">{assets.length}</p>
            <p className="text-sm text-muted-foreground">Total knowledge records</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Ready</p>
            <p className="mt-1 text-2xl font-semibold">{readyAssets}</p>
            <p className="text-sm text-muted-foreground">Approved for retrieval</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Pending Review</p>
            <p className="mt-1 text-2xl font-semibold">{pendingReviewAssets}</p>
            <p className="text-sm text-muted-foreground">Awaiting approval</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Rejected</p>
            <p className="mt-1 text-2xl font-semibold">{rejectedAssets}</p>
            <p className="text-sm text-muted-foreground">Excluded from retrieval</p>
          </div>
          <div className="rounded-md border p-3">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">Chunks</p>
            <p className="mt-1 text-2xl font-semibold">{totalChunks}</p>
            <p className="text-sm text-muted-foreground">Indexed retrieval chunks</p>
          </div>
        </div>
        <div className="rounded-md border p-3 text-sm text-muted-foreground">
          <p>
            <span className="font-medium text-foreground">Source mix:</span>{" "}
            {sourceSummary || "No indexed sources yet."}
          </p>
          <p className="mt-1">
            <span className="font-medium text-foreground">Latest update:</span>{" "}
            {formatTimestamp(latestAssetTimestamp)}
          </p>
          {lastSyncResult ? (
            <p className="mt-1">
              <span className="font-medium text-foreground">Last Jira sync:</span>{" "}
              {`created ${lastSyncResult.created_assets}, updated ${lastSyncResult.updated_assets}, unchanged ${lastSyncResult.unchanged_assets}, deleted ${lastSyncResult.deleted_assets}, failed ${lastSyncResult.failed_assets}`}
            </p>
          ) : null}
          {runtimeStatus ? (
            <>
              <p className="mt-1">
                <span className="font-medium text-foreground">Hourly sync state:</span>{" "}
                {runtimeStatus.state}
                {runtimeStatus.failure_category ? ` (${runtimeStatus.failure_category})` : ""}
              </p>
              <p className="mt-1">
                <span className="font-medium text-foreground">Last successful hourly sync:</span>{" "}
                {formatTimestamp(runtimeStatus.last_successful_sync_at)}
              </p>
              <p className="mt-1">
                <span className="font-medium text-foreground">Next retry:</span>{" "}
                {formatTimestamp(runtimeStatus.next_retry_at)}
              </p>
              {runtimeStatus.last_error ? (
                <p className="mt-1">
                  <span className="font-medium text-foreground">Hourly sync error:</span>{" "}
                  {runtimeStatus.last_error}
                </p>
              ) : null}
            </>
          ) : null}
        </div>
        <input
          ref={fileInputRef}
          className="hidden"
          type="file"
          multiple
          onChange={onFileInputChange}
          disabled={uploadingFiles}
        />
        <div
          className={`rounded-md border border-dashed p-6 text-center ${
            draggingOver ? "border-primary bg-primary/5" : "border-muted-foreground/30"
          }`}
          onDragOver={(event) => {
            event.preventDefault();
            if (!uploadingFiles) {
              setDraggingOver(true);
            }
          }}
          onDragLeave={(event) => {
            event.preventDefault();
            setDraggingOver(false);
          }}
          onDrop={onDrop}
        >
          <p className="text-sm text-muted-foreground">
            {uploadingFiles ? "Uploading files..." : "Drag and drop files here"}
          </p>
          <div className="mt-3">
            <Button
              variant="outline"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadingFiles}
            >
              Choose files
            </Button>
          </div>
        </div>
        {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
        {assets.length === 0 ? (
          <p className="rounded-md border p-3 text-sm text-muted-foreground">
            {loadingAssets ? "Loading files..." : "No files uploaded yet."}
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Updated</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {assets.map((asset) => (
                <TableRow key={asset.asset_id}>
                  <TableCell className="font-medium">{asset.title}</TableCell>
                  <TableCell>{asset.status}</TableCell>
                  <TableCell>{formatTimestamp(asset.updated_at)}</TableCell>
                  <TableCell className="text-right">
                    {asset.status === "pending_review" ? (
                      <>
                        <Button
                          size="sm"
                          variant="outline"
                          className="mr-2"
                          disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                          onClick={() => void updateAssetStatus(asset.asset_id, "ready")}
                        >
                          {updatingAssetId === asset.asset_id ? "Saving..." : "Approve"}
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          className="mr-2"
                          disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                          onClick={() => void updateAssetStatus(asset.asset_id, "rejected")}
                        >
                          {updatingAssetId === asset.asset_id ? "Saving..." : "Reject"}
                        </Button>
                      </>
                    ) : null}
                    {asset.status === "rejected" ? (
                      <Button
                        size="sm"
                        variant="outline"
                        className="mr-2"
                        disabled={updatingAssetId === asset.asset_id || deletingAssetId === asset.asset_id}
                        onClick={() => void updateAssetStatus(asset.asset_id, "pending_review")}
                      >
                        {updatingAssetId === asset.asset_id ? "Saving..." : "Move to review"}
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="outline"
                      className="text-red-700 hover:text-red-800"
                      disabled={deletingAssetId === asset.asset_id}
                      onClick={() => void removeAsset(asset.asset_id)}
                    >
                      {deletingAssetId === asset.asset_id ? "Deleting..." : "Delete"}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
