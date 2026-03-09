"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { KeyRound, Pencil, RefreshCw, Save, SearchCheck, Trash2, X } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteTenantManagedSecret,
  listTenantManagedSecrets,
  resolveTenantManagedSecret,
  upsertTenantManagedSecret,
  type ManagedSecretRecord,
} from "@/lib/api";

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

export default function TenantSecretsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const tenantPrefix = useMemo(() => `tenant/${tenantId}/`, [tenantId]);

  const [items, setItems] = useState<ManagedSecretRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [editingSecretRef, setEditingSecretRef] = useState<string | null>(null);

  function fromScopedRef(ref: string): string {
    return ref.startsWith(tenantPrefix) ? ref.slice(tenantPrefix.length) : ref;
  }

  async function refresh(): Promise<void> {
    if (!credentials) return;
    setLoading(true);
    try {
      const refs = await listTenantManagedSecrets(credentials, tenantId);
      setItems(refs);
      setStatusLine(`Loaded ${refs.length} tenant secret(s).`);
    } catch (error) {
      setStatusLine(`Failed to load secrets: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [credentials, tenantPrefix]); // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSecret() {
    if (!credentials) return;
    if (!secretKey.trim() || !secretValue.trim()) {
      setStatusLine("Secret key and value are required.");
      return;
    }
    setSaving(true);
    try {
      const saved = await upsertTenantManagedSecret(credentials, tenantId, secretKey.trim(), secretValue);
      const wasEditing = editingSecretRef === saved.secret_ref;
      setSecretKey("");
      setSecretValue("");
      setEditingSecretRef(null);
      setStatusLine(`${wasEditing ? "Updated" : "Saved"} ${saved.secret_ref}.`);
      await refresh();
    } catch (error) {
      setStatusLine(`Save failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function checkResolution() {
    if (!credentials) return;
    if (!secretKey.trim()) {
      setStatusLine("Enter a secret key to resolve.");
      return;
    }
    setSaving(true);
    try {
      const result = await resolveTenantManagedSecret(credentials, tenantId, secretKey.trim());
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
      await deleteTenantManagedSecret(credentials, tenantId, fromScopedRef(secretRefToDelete));
      setStatusLine(`Deleted ${secretRefToDelete}.`);
      if (editingSecretRef === secretRefToDelete) {
        setEditingSecretRef(null);
        setSecretKey("");
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
    setSecretKey(fromScopedRef(secretRefToEdit));
    setSecretValue("");
    setEditingSecretRef(secretRefToEdit);
    setStatusLine(`Editing ${secretRefToEdit}. Existing value is never shown.`);
  }

  function cancelEditingSecret() {
    setEditingSecretRef(null);
    setSecretKey("");
    setSecretValue("");
    setStatusLine("");
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
            <KeyRound className="h-5 w-5 text-primary" />
          </div>
          <div>
            <h1 className="text-xl font-semibold">Tenant Secrets</h1>
            <p className="text-sm text-muted-foreground">
              Scoped to{" "}
              <code className="rounded bg-muted px-1 font-mono text-xs">{tenantPrefix}{"{"}"KEY{"}"}</code>.
              Values are encrypted at rest and never returned by the API.
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading}>
          <RefreshCw className={`mr-1.5 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </Button>
      </div>

      {statusLine ? (
        <p className="rounded-lg border bg-muted/40 px-4 py-2.5 text-sm text-muted-foreground">{statusLine}</p>
      ) : null}

      {/* Add / Edit card */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">
            {editingSecretRef ? "Edit Secret" : "Add Secret"}
          </CardTitle>
          {editingSecretRef ? (
            <p className="text-sm text-warning">
              Editing <code className="rounded bg-muted px-1 font-mono text-xs">{editingSecretRef}</code>.
              The existing value is never shown.
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">
              Keys are stored under <code className="rounded bg-muted px-1 font-mono text-xs">{tenantPrefix}{"{KEY}"}</code>.
            </p>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Secret key
              </label>
              <Input
                value={secretKey}
                onChange={(e) => setSecretKey(e.target.value)}
                placeholder="e.g. GITHUB_APP_ID"
                disabled={saving || Boolean(editingSecretRef)}
                className="font-mono"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
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
        </CardContent>
      </Card>

      {/* Secrets table */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-2">
            <CardTitle className="text-base">Stored Secrets</CardTitle>
            {items.length > 0 ? (
              <Badge variant="outline" className="text-xs">
                {items.length} secret{items.length !== 1 ? "s" : ""}
              </Badge>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {items.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
              <KeyRound className="h-6 w-6 text-muted-foreground" />
              <p className="text-sm font-medium">No tenant secrets stored yet</p>
              <p className="text-xs text-muted-foreground">Add a secret key and value above to get started.</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow className="bg-muted/40">
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
                      <div className="font-mono text-xs font-medium">{fromScopedRef(item.secret_ref)}</div>
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
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
