"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import { Pencil, RefreshCw, Save, SearchCheck, Trash2 } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteManagedSecret,
  listManagedSecrets,
  resolveManagedSecret,
  upsertManagedSecret,
  type ManagedSecretRecord
} from "@/lib/api";

function formatTimestamp(value: string | null): string {
  if (!value) {
    return "n/a";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

export default function TenantSecretsPage() {
  const params = useParams<{ tenantId: string }>();
  const { credentials } = useAuth();
  const tenantId = decodeURIComponent(params.tenantId);
  const tenantPrefix = useMemo(() => `tenant/${tenantId}/`, [tenantId]);

  const [items, setItems] = useState<ManagedSecretRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState(
    `Manage tenant-scoped refs under ${tenantPrefix}*.`
  );
  const [secretKey, setSecretKey] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const [editingSecretRef, setEditingSecretRef] = useState<string | null>(null);

  function toScopedRef(key: string): string {
    return `${tenantPrefix}${key.trim()}`;
  }

  function fromScopedRef(secretRef: string): string {
    return secretRef.startsWith(tenantPrefix) ? secretRef.slice(tenantPrefix.length) : secretRef;
  }

  async function refresh(): Promise<void> {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const refs = await listManagedSecrets(credentials);
      const scoped = refs.filter((item) => item.secret_ref.startsWith(tenantPrefix));
      setItems(scoped);
      setStatusLine(`Loaded ${scoped.length} tenant secret reference(s).`);
    } catch (error) {
      setStatusLine(`Failed to load tenant secrets: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [credentials, tenantPrefix]); // eslint-disable-line react-hooks/exhaustive-deps

  async function saveSecret() {
    if (!credentials) {
      return;
    }
    if (!secretKey.trim() || !secretValue.trim()) {
      setStatusLine("Secret key and value are required.");
      return;
    }

    setSaving(true);
    try {
      const scopedRef = toScopedRef(secretKey);
      const saved = await upsertManagedSecret(credentials, scopedRef, secretValue);
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
    if (!credentials) {
      return;
    }
    if (!secretKey.trim()) {
      setStatusLine("Enter a secret key to resolve.");
      return;
    }

    setSaving(true);
    try {
      const result = await resolveManagedSecret(credentials, toScopedRef(secretKey));
      setStatusLine(
        `Resolution for ${result.secret_ref}: ${result.resolved ? "resolved" : "missing"} (source=${result.source}).`
      );
    } catch (error) {
      setStatusLine(`Resolve failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  async function removeSecret(secretRefToDelete: string) {
    if (!credentials) {
      return;
    }
    if (!window.confirm(`Delete secret '${secretRefToDelete}'? This cannot be undone.`)) {
      return;
    }
    setSaving(true);
    try {
      await deleteManagedSecret(credentials, secretRefToDelete);
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
    setStatusLine("Edit cancelled.");
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Tenant Secrets: {tenantId}</CardTitle>
        <CardDescription>
          Tenant-scoped refs are stored as <code className="font-mono">{tenantPrefix}{"{KEY}"}</code>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto_auto]">
          <Input
            placeholder="Secret key (example: GITHUB_APP_ID)"
            value={secretKey}
            onChange={(event) => setSecretKey(event.target.value)}
          />
          <Textarea
            placeholder="Secret value"
            value={secretValue}
            onChange={(event) => setSecretValue(event.target.value)}
            className="min-h-[88px] font-mono text-xs"
          />
          <Button onClick={() => void saveSecret()} disabled={saving}>
            <Save className="mr-2 h-4 w-4" />
            {editingSecretRef ? "Update" : "Save"}
          </Button>
          <Button variant="outline" onClick={() => void checkResolution()} disabled={saving}>
            <SearchCheck className="mr-2 h-4 w-4" />
            Resolve
          </Button>
        </div>

        {editingSecretRef ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>
              Editing <strong>{editingSecretRef}</strong>.
            </span>
            <Button type="button" variant="ghost" size="sm" onClick={cancelEditingSecret} disabled={saving}>
              Cancel
            </Button>
          </div>
        ) : null}

        <div className="flex items-center justify-between">
          <p className="text-sm text-muted-foreground">{statusLine}</p>
          <Button variant="outline" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw className="mr-2 h-4 w-4" />
            Refresh
          </Button>
        </div>

        <div className="overflow-x-auto rounded-md border">
          <table className="w-full min-w-[560px] text-left text-sm">
            <thead className="bg-muted/60 text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Key</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">Updated</th>
                <th className="px-3 py-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 ? (
                <tr>
                  <td className="px-3 py-3 text-muted-foreground" colSpan={4}>
                    No tenant-managed secrets stored yet.
                  </td>
                </tr>
              ) : (
                items.map((item) => (
                  <tr key={item.secret_ref} className="border-t">
                    <td className="px-3 py-2">
                      <div className="font-mono text-xs">{fromScopedRef(item.secret_ref)}</div>
                      <div className="text-[11px] text-muted-foreground">{item.secret_ref}</div>
                    </td>
                    <td className="px-3 py-2">{item.source}</td>
                    <td className="px-3 py-2">{formatTimestamp(item.updated_at)}</td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <Button variant="ghost" size="sm" onClick={() => startEditingSecret(item.secret_ref)} disabled={saving}>
                          <Pencil className="mr-2 h-4 w-4" />
                          Edit
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => void removeSecret(item.secret_ref)} disabled={saving}>
                          <Trash2 className="mr-2 h-4 w-4" />
                          Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
