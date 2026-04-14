"use client";

import { useEffect, useMemo, useState } from "react";
import { KeyRound, Pencil, RefreshCw, Save, SearchCheck, Trash2, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteManagedSecret,
  listManagedSecrets,
  resolveManagedSecret,
  upsertManagedSecret,
  type ManagedSecretRecord,
} from "@/lib/api";
import { formatTimestamp } from "@/lib/datetime";

function toPlatformRef(rawRef: string): string {
  const normalized = rawRef.trim();
  if (!normalized) return "";
  if (normalized.startsWith("tenant/") || normalized.startsWith("project/")) {
    throw new Error("Platform secrets page only supports platform/* refs.");
  }
  return normalized.startsWith("platform/") ? normalized : `platform/${normalized}`;
}

function fromPlatformRef(ref: string): string {
  return ref.startsWith("platform/") ? ref.slice("platform/".length) : ref;
}

export default function SecretsPage() {
  const { credentials } = useAuth();

  const [items, setItems] = useState<ManagedSecretRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [secretRef, setSecretRef] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [editingSecretRef, setEditingSecretRef] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    if (!credentials) return;
    setLoading(true);
    try {
      const refs = await listManagedSecrets(credentials);
      setItems(refs);
      setStatusLine("");
    } catch (error) {
      setStatusLine(`Failed to load secrets: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [credentials]); // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSecret() {
    if (!credentials) return;
    if (!secretRef.trim() || !secretValue.trim()) {
      setStatusLine("Secret key and value are required.");
      return;
    }
    setSaving(true);
    try {
      const scopedRef = toPlatformRef(secretRef);
      if (!scopedRef) {
        setStatusLine("Secret key and value are required.");
        return;
      }
      await upsertManagedSecret(credentials, scopedRef, secretValue);
      setSecretRef("");
      setSecretValue("");
      setEditingSecretRef(null);
      setStatusLine("");
      await refresh();
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function checkResolution() {
    if (!credentials) return;
    if (!secretRef.trim()) {
      setStatusLine("Enter a secret key to resolve.");
      return;
    }
    setSaving(true);
    try {
      const scopedRef = toPlatformRef(secretRef);
      if (!scopedRef) {
        setStatusLine("Enter a secret key to resolve.");
        return;
      }
      const result = await resolveManagedSecret(credentials, scopedRef);
      setStatusLine(
        `${result.secret_ref}: ${result.resolved ? "resolved" : "missing"} (source: ${result.source})`
      );
    } catch (error) {
      setStatusLine(`Resolve failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function removeSecret(secretRefToDelete: string) {
    if (!credentials) return;
    if (!window.confirm(`Delete '${secretRefToDelete}'? This cannot be undone.`)) return;
    setSaving(true);
    try {
      await deleteManagedSecret(credentials, secretRefToDelete);
      setStatusLine("");
      if (secretRef === secretRefToDelete) setSecretRef("");
      if (editingSecretRef === secretRefToDelete) {
        setEditingSecretRef(null);
        setSecretValue("");
      }
      await refresh();
    } catch (error) {
      setStatusLine(`Delete failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  function startEditingSecret(secretRefToEdit: string) {
    setSecretRef(fromPlatformRef(secretRefToEdit));
    setSecretValue("");
    setEditingSecretRef(secretRefToEdit);
    setStatusLine(`Editing ${secretRefToEdit}. Enter a new value to replace the stored secret.`);
  }

  function cancelEditingSecret() {
    setEditingSecretRef(null);
    setSecretRef("");
    setSecretValue("");
    setStatusLine("");
  }

  const managedCount = items.filter((i) => i.source === "managed").length;

  const statusClasses = useMemo(() => {
    const n = statusLine.toLowerCase();
    if (n.includes("failed") || n.includes("unable")) return "border-red-300 bg-red-50 text-red-800";
    if (n.includes("editing")) return "border-amber-300 bg-amber-50 text-amber-800";
    return "border-muted bg-muted/30 text-muted-foreground";
  }, [statusLine]);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-end">
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {statusLine ? (
        <div className={`rounded-xl border px-4 py-3 text-sm ${statusClasses}`}>{statusLine}</div>
      ) : null}

      <div className="overflow-hidden rounded-2xl border bg-background">
        <div className="divide-y">
          <section className="p-6">
            <div>
              <h2 className="text-base font-semibold">
                {editingSecretRef ? "Edit Secret" : "Add Secret"}
              </h2>
              {editingSecretRef ? (
                <p className="mt-1 text-sm text-warning">
                  Editing <code className="rounded bg-muted px-1 font-mono text-xs">{editingSecretRef}</code>.
                </p>
              ) : null}
            </div>
            <div className="mt-4 space-y-4">
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">Secret key</label>
                  <Input
                    value={secretRef}
                    onChange={(e) => setSecretRef(e.target.value)}
                    placeholder="e.g. DISCORD_BOT_TOKEN"
                    disabled={saving || Boolean(editingSecretRef)}
                    className="font-mono"
                  />
                </div>
                <div className="space-y-1.5">
                  <label className="text-sm font-medium">
                    {editingSecretRef ? "New value" : "Secret value"}
                  </label>
                  <Textarea
                    value={secretValue}
                    onChange={(e) => setSecretValue(e.target.value)}
                    placeholder="Paste secret value here"
                    className="min-h-[72px] font-mono text-xs"
                    disabled={saving}
                  />
                </div>
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" onClick={() => void saveSecret()} disabled={saving}>
                  <Save className="mr-1.5 h-3.5 w-3.5" />
                  {editingSecretRef ? "Update" : "Save secret"}
                </Button>
                <Button size="sm" variant="outline" onClick={() => void checkResolution()} disabled={saving}>
                  <SearchCheck className="mr-1.5 h-3.5 w-3.5" />
                  Resolve
                </Button>
                {editingSecretRef ? (
                  <Button size="sm" variant="ghost" onClick={cancelEditingSecret} disabled={saving}>
                    <X className="mr-1.5 h-3.5 w-3.5" />
                    Cancel edit
                  </Button>
                ) : null}
              </div>
            </div>
          </section>

          <section className="p-6 pb-0">
            <div className="flex items-center justify-between gap-2">
              <h2 className="text-base font-semibold">Stored Secrets</h2>
              {items.length > 0 ? (
                <span className="text-xs text-muted-foreground">
                  {managedCount} managed · {items.length - managedCount} inherited
                </span>
              ) : null}
            </div>
          </section>

          <div>
            {items.length === 0 ? (
              <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
                <KeyRound className="h-6 w-6 text-muted-foreground" />
                <p className="text-sm font-medium">No platform secrets stored yet</p>
                <p className="text-xs text-muted-foreground">Add a secret key and value above to get started.</p>
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="pl-6">Key</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead>Updated</TableHead>
                    <TableHead className="text-right pr-6">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((item) => (
                    <TableRow
                      key={item.secret_ref}
                      className={editingSecretRef === item.secret_ref ? "bg-warning/5" : undefined}
                    >
                      <TableCell className="pl-6">
                        <div className="font-mono text-xs font-medium">{fromPlatformRef(item.secret_ref)}</div>
                        <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{item.secret_ref}</div>
                      </TableCell>
                      <TableCell>
                        <Badge variant={item.source === "managed" ? "default" : "outline"} className="text-[11px]">
                          {item.source}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-sm text-muted-foreground whitespace-nowrap">
                        {formatTimestamp(item.updated_at)}
                      </TableCell>
                      <TableCell className="text-right pr-6">
                        {item.source === "managed" ? (
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 px-2 text-xs"
                              onClick={() => startEditingSecret(item.secret_ref)}
                              disabled={saving}
                            >
                              <Pencil className="mr-1 h-3 w-3" />
                              Edit
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                              onClick={() => void removeSecret(item.secret_ref)}
                              disabled={saving}
                            >
                              <Trash2 className="mr-1 h-3 w-3" />
                              Delete
                            </Button>
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">Read-only</span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
