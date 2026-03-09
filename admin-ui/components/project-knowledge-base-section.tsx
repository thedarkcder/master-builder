"use client";

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  createProjectKnowledgeAsset,
  deleteProjectKnowledgeAsset,
  listProjectKnowledgeAssets,
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
  const [draggingOver, setDraggingOver] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

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
            <CardDescription>Drop files to upload. Uploaded files are listed below.</CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={() => void loadAssets()} disabled={loadingAssets}>
            {loadingAssets ? "Refreshing..." : "Refresh"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
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
