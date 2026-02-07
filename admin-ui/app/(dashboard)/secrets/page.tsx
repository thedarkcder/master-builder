"use client";

import { useEffect, useState } from "react";
import { RefreshCw, Save, SearchCheck } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
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

export default function SecretsPage() {
  const { credentials } = useAuth();
  const [items, setItems] = useState<ManagedSecretRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [statusLine, setStatusLine] = useState("Store secrets by reference; values are encrypted at rest.");
  const [secretRef, setSecretRef] = useState("");
  const [secretValue, setSecretValue] = useState("");

  async function refresh(): Promise<void> {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const refs = await listManagedSecrets(credentials);
      setItems(refs);
      setStatusLine(`Loaded ${refs.length} managed secret reference(s).`);
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
    if (!credentials) {
      return;
    }
    if (!secretRef.trim() || !secretValue.trim()) {
      setStatusLine("Secret ref and value are required.");
      return;
    }

    setSaving(true);
    try {
      const saved = await upsertManagedSecret(credentials, secretRef.trim(), secretValue);
      setSecretValue("");
      setStatusLine(`Saved ${saved.secret_ref} (${saved.source}).`);
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
    if (!secretRef.trim()) {
      setStatusLine("Enter a secret ref to resolve.");
      return;
    }

    setSaving(true);
    try {
      const result = await resolveManagedSecret(credentials, secretRef.trim());
      setStatusLine(
        `Resolution for ${result.secret_ref}: ${result.resolved ? "resolved" : "missing"} (source=${result.source}).`
      );
    } catch (error) {
      setStatusLine(`Resolve failed: ${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Secrets Manager</CardTitle>
        <CardDescription>
          Manage secret refs used by Jira OAuth, GitHub App credentials, and webhook validation. Secret values are not
          returned by the API.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto_auto]">
          <Input
            value={secretRef}
            onChange={(event) => setSecretRef(event.target.value)}
            placeholder="MB_GH_APP_ID or secret/jira-client-id"
          />
          <Input
            value={secretValue}
            onChange={(event) => setSecretValue(event.target.value)}
            placeholder="Secret value"
            type="password"
          />
          <Button onClick={() => void saveSecret()} disabled={saving}>
            <Save className="mr-2 h-4 w-4" />
            Save
          </Button>
          <Button variant="outline" onClick={() => void checkResolution()} disabled={saving}>
            <SearchCheck className="mr-2 h-4 w-4" />
            Resolve
          </Button>
        </div>

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
                <th className="px-3 py-2 font-medium">Secret Ref</th>
                <th className="px-3 py-2 font-medium">Source</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {items.length === 0 ? (
                <tr>
                  <td className="px-3 py-3 text-muted-foreground" colSpan={3}>
                    No managed secrets stored yet.
                  </td>
                </tr>
              ) : (
                items.map((item) => (
                  <tr key={item.secret_ref} className="border-t">
                    <td className="px-3 py-2 font-mono text-xs">{item.secret_ref}</td>
                    <td className="px-3 py-2">{item.source}</td>
                    <td className="px-3 py-2">{formatTimestamp(item.updated_at)}</td>
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
