"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ExternalLink, Save } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useToast } from "@/components/ui/toast-provider";
import {
  createArchitectureDocument,
  getProject,
  listArchitectureDocuments,
  updateArchitectureDocument,
  type ArchitectureDocumentRecord,
  type ProjectRecord
} from "@/lib/api";

type DocumentFormState = {
  title: string;
  status: "draft" | "ready" | "superseded";
  canonical_url: string;
  provider_ref: string;
  content_markdown: string;
};

function buildDocumentForm(document: ArchitectureDocumentRecord | null): DocumentFormState {
  return {
    title: document?.title ?? "",
    status: document?.status ?? "draft",
    canonical_url: document?.canonical_url ?? "",
    provider_ref: document?.provider_ref ?? "",
    content_markdown: document?.content_markdown ?? ""
  };
}

export function ProjectArchitectureDocumentsPage() {
  const params = useParams<{ tenantId: string; projectId: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const { credentials } = useAuth();
  const { showToast } = useToast();

  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [documents, setDocuments] = useState<ArchitectureDocumentRecord[]>([]);
  const [selectedDocumentId, setSelectedDocumentId] = useState("");
  const [createParentIssueKey, setCreateParentIssueKey] = useState("");
  const [createIssueSummary, setCreateIssueSummary] = useState("");
  const [createTitle, setCreateTitle] = useState("");
  const [form, setForm] = useState<DocumentFormState>(() => buildDocumentForm(null));
  const [busy, setBusy] = useState(false);
  const [statusLine, setStatusLine] = useState("");

  const provider = project?.architecture_docs?.provider ?? null;
  const selectedDocument = useMemo(
    () => documents.find((item) => item.document_id === selectedDocumentId) ?? null,
    [documents, selectedDocumentId]
  );

  useEffect(() => {
    if (!credentials) return;
    const auth = credentials;
    let cancelled = false;
    async function load() {
      setBusy(true);
      try {
        const [projectPayload, docsPayload] = await Promise.all([
          getProject(auth, params.tenantId, params.projectId),
          listArchitectureDocuments(auth, params.tenantId, params.projectId)
        ]);
        if (cancelled) return;
        setProject(projectPayload);
        setDocuments(docsPayload.items);
        const requestedDocumentId = searchParams.get("documentId")?.trim() ?? "";
        const initialDocument =
          docsPayload.items.find((item) => item.document_id === requestedDocumentId) ?? docsPayload.items[0] ?? null;
        setSelectedDocumentId(initialDocument?.document_id ?? "");
        setForm(buildDocumentForm(initialDocument));
        setStatusLine("");
      } catch (error) {
        if (!cancelled) {
          setStatusLine(`Failed to load architecture documents: ${(error as Error).message}`);
        }
      } finally {
        if (!cancelled) {
          setBusy(false);
        }
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [credentials, params.projectId, params.tenantId, searchParams]);

  useEffect(() => {
    setForm(buildDocumentForm(selectedDocument));
  }, [selectedDocument]);

  async function refreshDocuments(nextSelectedId?: string) {
    if (!credentials) return;
    const auth = credentials;
    const docsPayload = await listArchitectureDocuments(auth, params.tenantId, params.projectId);
    setDocuments(docsPayload.items);
    const nextSelected =
      docsPayload.items.find((item) => item.document_id === (nextSelectedId || selectedDocumentId)) ??
      docsPayload.items[0] ??
      null;
    setSelectedDocumentId(nextSelected?.document_id ?? "");
    setForm(buildDocumentForm(nextSelected));
  }

  async function handleCreateDocument() {
    if (!credentials || !provider) return;
    const auth = credentials;
    if (!createParentIssueKey.trim()) {
      setStatusLine("Parent issue key is required.");
      return;
    }
    setBusy(true);
    try {
      const created = await createArchitectureDocument(auth, params.tenantId, params.projectId, {
        parent_issue_key: createParentIssueKey.trim().toUpperCase(),
        issue_summary: createIssueSummary.trim() || null,
        title: createTitle.trim() || null,
        canonical_url: null,
        provider_ref: null
      });
      await refreshDocuments(created.document_id);
      setCreateParentIssueKey("");
      setCreateIssueSummary("");
      setCreateTitle("");
      showToast({ title: "Architecture document created", description: created.title, tone: "success" });
      router.replace(
        `/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/architecture?documentId=${encodeURIComponent(created.document_id)}`
      );
    } catch (error) {
      showToast({ title: "Architecture document create failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveDocument() {
    if (!credentials || !selectedDocument) return;
    const auth = credentials;
    setBusy(true);
    try {
      const updated = await updateArchitectureDocument(
        auth,
        params.tenantId,
        params.projectId,
        selectedDocument.document_id,
        {
          title: form.title.trim(),
          status: form.status,
          canonical_url: null,
          provider_ref: null,
          content_markdown: selectedDocument.provider === "internal" ? form.content_markdown : null
        }
      );
      await refreshDocuments(updated.document_id);
      showToast({ title: "Architecture document saved", description: updated.title, tone: "success" });
    } catch (error) {
      showToast({ title: "Architecture document save failed", description: (error as Error).message, tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="space-y-1">
          <Button asChild variant="ghost" size="sm" className="-ml-2">
            <Link href={`/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}`}>
              <ArrowLeft className="mr-1.5 h-3.5 w-3.5" />
              Back to project
            </Link>
          </Button>
          <h1 className="text-2xl font-semibold tracking-tight">Architecture documents</h1>
          <p className="text-sm text-muted-foreground">
            Architecture is persistent project knowledge. Jira tickets should link here instead of storing architecture
            inline.
          </p>
        </div>
        {selectedDocument?.canonical_url ? (
          <Button asChild variant="outline" size="sm">
            <Link href={selectedDocument.canonical_url} target="_blank">
              Open document
              <ExternalLink className="ml-1.5 h-3.5 w-3.5" />
            </Link>
          </Button>
        ) : null}
      </div>

      {statusLine ? <div className="rounded-lg border px-3 py-2 text-sm">{statusLine}</div> : null}

      <div className="grid gap-6 lg:grid-cols-[320px_minmax(0,1fr)]">
        <div className="space-y-6">
          <div className="rounded-2xl border bg-background p-5">
            <h2 className="text-sm font-semibold">Project configuration</h2>
            <div className="mt-3 space-y-2 text-sm text-muted-foreground">
              <p>Provider: {provider ?? "not configured"}</p>
              {project?.architecture_docs?.space_key ? <p>Space key: {project.architecture_docs.space_key}</p> : null}
              {project?.architecture_docs?.parent_page_id ? (
                <p>Parent page ID: {project.architecture_docs.parent_page_id}</p>
              ) : null}
            </div>
          </div>

          <div className="rounded-2xl border bg-background p-5">
            <h2 className="text-sm font-semibold">Create document</h2>
            <div className="mt-4 space-y-3">
              <Input
                value={createParentIssueKey}
                onChange={(e) => setCreateParentIssueKey(e.target.value)}
                placeholder="Parent issue key"
                disabled={busy || !provider}
              />
              <Input
                value={createIssueSummary}
                onChange={(e) => setCreateIssueSummary(e.target.value)}
                placeholder="Epic summary"
                disabled={busy || !provider}
              />
              <Input
                value={createTitle}
                onChange={(e) => setCreateTitle(e.target.value)}
                placeholder="Document title"
                disabled={busy || !provider}
              />
              {provider === "confluence" ? (
                <p className="text-xs text-muted-foreground">
                  Confluence documents are created from the configured space and optional parent page.
                </p>
              ) : null}
              <Button onClick={handleCreateDocument} disabled={busy || !provider}>
                Create architecture document
              </Button>
            </div>
          </div>

          <div className="rounded-2xl border bg-background p-5">
            <h2 className="text-sm font-semibold">Documents</h2>
            <div className="mt-4 space-y-2">
              {documents.map((document) => (
                <button
                  key={document.document_id}
                  type="button"
                  onClick={() => {
                    setSelectedDocumentId(document.document_id);
                    router.replace(
                      `/${encodeURIComponent(params.tenantId)}/projects/${encodeURIComponent(params.projectId)}/architecture?documentId=${encodeURIComponent(document.document_id)}`
                    );
                  }}
                  className={`w-full rounded-xl border px-3 py-3 text-left ${
                    document.document_id === selectedDocumentId ? "border-primary bg-primary/5" : "border-border"
                  }`}
                >
                  <p className="text-sm font-medium text-foreground">{document.title}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {document.parent_issue_key} · {document.provider} · {document.status}
                  </p>
                </button>
              ))}
              {documents.length === 0 ? (
                <p className="text-sm text-muted-foreground">No architecture documents exist for this project yet.</p>
              ) : null}
            </div>
          </div>
        </div>

        <div className="rounded-2xl border bg-background p-6">
          {selectedDocument ? (
            <div className="space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <Input
                  value={form.title}
                  onChange={(e) => setForm((prev) => ({ ...prev, title: e.target.value }))}
                  disabled={busy}
                  placeholder="Document title"
                />
                <select
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={form.status}
                  onChange={(e) =>
                    setForm((prev) => ({
                      ...prev,
                      status: e.target.value as DocumentFormState["status"]
                    }))
                  }
                  disabled={busy}
                >
                  <option value="draft">Draft</option>
                  <option value="ready">Ready</option>
                  <option value="superseded">Superseded</option>
                </select>
              </div>
              {selectedDocument.provider === "confluence" ? (
                <div className="grid gap-3 rounded-xl border p-4 text-sm text-muted-foreground">
                  <p>Confluence page URL: {selectedDocument.canonical_url}</p>
                  <p>Confluence page ID: {selectedDocument.provider_ref ?? "unknown"}</p>
                  <p>Confluence page content is edited in Confluence. The platform only tracks readiness and title.</p>
                </div>
              ) : (
                <Textarea
                  value={form.content_markdown}
                  onChange={(e) => setForm((prev) => ({ ...prev, content_markdown: e.target.value }))}
                  disabled={busy}
                  className="min-h-[520px] font-mono text-sm"
                />
              )}
              <div className="flex items-center gap-3">
                <Button onClick={handleSaveDocument} disabled={busy || !form.title.trim()}>
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  Save document
                </Button>
                <p className="text-xs text-muted-foreground">
                  Parent issue: {selectedDocument.parent_issue_key} · URL: {selectedDocument.canonical_url}
                </p>
              </div>
            </div>
          ) : (
            <div className="text-sm text-muted-foreground">Select an architecture document to edit.</div>
          )}
        </div>
      </div>
    </div>
  );
}
