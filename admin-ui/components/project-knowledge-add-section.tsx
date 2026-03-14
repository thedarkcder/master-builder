"use client";

import { useCallback, useRef, useState, type ChangeEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { createProjectKnowledgeAsset, type Credentials } from "@/lib/api";

type ProjectKnowledgeAddSectionProps = {
  credentials: Credentials | null;
  tenantId: string;
  projectId: string;
};

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

export function ProjectKnowledgeAddSection({
  credentials,
  tenantId,
  projectId,
}: ProjectKnowledgeAddSectionProps) {
  const [uploadingFiles, setUploadingFiles] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const ensureCredentials = useCallback((): Credentials | null => {
    if (!credentials) {
      setStatusLine("Admin session is not ready yet. Wait a moment and try again.");
      return null;
    }
    return credentials;
  }, [credentials]);

  async function uploadFiles(fileList: FileList | null) {
    const activeCredentials = ensureCredentials();
    if (!activeCredentials || !fileList || fileList.length === 0) {
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
          await createProjectKnowledgeAsset(activeCredentials, tenantId, projectId, {
            title: file.name,
            source_type: "file_upload",
            mime_type: file.type || "application/octet-stream",
            source_ref: file.name,
            source_timestamp: new Date(file.lastModified).toISOString(),
            content_base64: contentBase64,
          });
          uploaded += 1;
        } catch {
          failed += 1;
        }
      }
      setStatusLine(
        failed === 0
          ? `Uploaded ${uploaded} file${uploaded === 1 ? "" : "s"}.`
          : `Uploaded ${uploaded} file${uploaded === 1 ? "" : "s"}, failed ${failed}.`,
      );
    } catch (error) {
      setStatusLine(`Unable to upload files: ${(error as Error).message}`);
    } finally {
      setUploadingFiles(false);
    }
  }

  function onFileInputChange(event: ChangeEvent<HTMLInputElement>) {
    void uploadFiles(event.target.files);
    event.target.value = "";
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Knowledge</CardTitle>
        <CardDescription>
          Upload files directly into the project knowledge store. Managed connectors and sync controls live in the Sources tab.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-md border p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-medium">Upload files</p>
              <p className="text-sm text-muted-foreground">
                Add documents, notes, exported text, or other source material to the project knowledge store.
              </p>
            </div>
            <div className="flex gap-2">
              <input
                ref={fileInputRef}
                className="hidden"
                type="file"
                multiple
                onChange={onFileInputChange}
                disabled={uploadingFiles}
              />
              <Button onClick={() => fileInputRef.current?.click()} disabled={uploadingFiles || !credentials}>
                {uploadingFiles ? "Uploading..." : "Upload files"}
              </Button>
            </div>
          </div>
        </div>

        {statusLine ? <p className="rounded-md border px-3 py-2 text-sm text-muted-foreground">{statusLine}</p> : null}
      </CardContent>
    </Card>
  );
}
