"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  createProjectKnowledgeAsset,
  deleteProjectKnowledgeAsset,
  listProjectKnowledgeAssets,
  syncProjectKnowledgeFromJira,
  type Credentials,
  type ProjectKnowledgeAssetRecord
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

export function ProjectKnowledgeBaseSection({
  credentials,
  tenantId,
  projectId
}: ProjectKnowledgeBaseSectionProps) {
  const [assets, setAssets] = useState<ProjectKnowledgeAssetRecord[]>([]);
  const [loadingAssets, setLoadingAssets] = useState(false);
  const [creatingAsset, setCreatingAsset] = useState(false);
  const [syncingFromJira, setSyncingFromJira] = useState(false);
  const [deletingAssetId, setDeletingAssetId] = useState<string | null>(null);
  const [titleInput, setTitleInput] = useState("");
  const [textInput, setTextInput] = useState("");
  const [statusLine, setStatusLine] = useState("");

  const loadAssets = useCallback(async () => {
    if (!credentials) {
      return;
    }
    setLoadingAssets(true);
    try {
      const payload = await listProjectKnowledgeAssets(credentials, tenantId, projectId);
      setAssets(payload);
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

  async function createTextAsset() {
    if (!credentials) {
      return;
    }
    if (!titleInput.trim() || !textInput.trim()) {
      setStatusLine("Title and text are required to create a knowledge asset.");
      return;
    }
    setCreatingAsset(true);
    try {
      await createProjectKnowledgeAsset(credentials, tenantId, projectId, {
        title: titleInput.trim(),
        mime_type: "text/plain",
        source_type: "manual",
        text_content: textInput.trim()
      });
      setTitleInput("");
      setTextInput("");
      setStatusLine("Knowledge asset created.");
      await loadAssets();
    } catch (error) {
      setStatusLine(`Unable to create knowledge asset: ${(error as Error).message}`);
    } finally {
      setCreatingAsset(false);
    }
  }

  async function syncFromJira() {
    if (!credentials) {
      return;
    }
    setSyncingFromJira(true);
    try {
      const result = await syncProjectKnowledgeFromJira(credentials, tenantId, projectId);
      setStatusLine(result.details || "Jira knowledge sync started.");
      await loadAssets();
    } catch (error) {
      setStatusLine(`Unable to sync knowledge from Jira: ${(error as Error).message}`);
    } finally {
      setSyncingFromJira(false);
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

  return (
    <div className="space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Knowledge Base</p>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void loadAssets()} disabled={loadingAssets}>
            {loadingAssets ? "Refreshing..." : "Refresh assets"}
          </Button>
          <Button variant="outline" size="sm" onClick={() => void syncFromJira()} disabled={syncingFromJira || loadingAssets}>
            {syncingFromJira ? "Syncing from Jira..." : "Sync from Jira"}
          </Button>
        </div>
      </div>
      {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
      <div className="grid gap-2 md:grid-cols-2">
        <Input
          value={titleInput}
          onChange={(event) => setTitleInput(event.target.value)}
          placeholder="Asset title"
          disabled={creatingAsset}
        />
        <div className="flex justify-start md:justify-end">
          <Button onClick={() => void createTextAsset()} disabled={creatingAsset || loadingAssets} className="w-full md:w-auto">
            {creatingAsset ? "Creating..." : "Create text asset"}
          </Button>
        </div>
      </div>
      <Textarea
        value={textInput}
        onChange={(event) => setTextInput(event.target.value)}
        placeholder="Paste knowledge text content"
        disabled={creatingAsset}
      />
      {assets.length === 0 ? (
        <p className="rounded-md border p-3 text-sm text-muted-foreground">
          {loadingAssets ? "Loading knowledge assets..." : "No knowledge assets for this project yet."}
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Title</TableHead>
              <TableHead>Source Type</TableHead>
              <TableHead>Chunks</TableHead>
              <TableHead>Updated</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {assets.map((asset) => (
              <TableRow key={asset.asset_id}>
                <TableCell className="font-medium">{asset.title}</TableCell>
                <TableCell>{asset.source_type}</TableCell>
                <TableCell>{asset.chunk_count}</TableCell>
                <TableCell>{formatTimestamp(asset.updated_at)}</TableCell>
                <TableCell>
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
    </div>
  );
}
