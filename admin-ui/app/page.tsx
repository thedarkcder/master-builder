"use client";

import { useMemo, useState } from "react";
import { CheckCircle2, Loader2, RefreshCw, XCircle } from "lucide-react";

import {
  createTenant,
  deleteTenant,
  type Credentials,
  getRun,
  getTenant,
  listRuns,
  listTenants,
  testGithub,
  testJira,
  updateTenant,
  type RunRecord,
  type TenantRecord
} from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

const tenantTemplate = {
  tenant_id: "tenant-demo",
  name: "Tenant Demo",
  is_enabled: true,
  jira: {
    mcp_endpoint: "https://mcp.example.test",
    auth_ref: "secret/jira",
    project_keys: ["TP"],
    ready_label: "agent:ready",
    in_progress_label: "agent:in-progress",
    blocked_label: "agent:blocked",
    done_label: "agent:done",
    ready_jql: "project = TP",
    webhook_secret_ref: null
  },
  github: {
    mode: "github_app",
    app_id_ref: "secret/app-id",
    private_key_ref: "secret/private-key",
    webhook_secret_ref: null,
    installation_id: "12345"
  },
  repos: {
    allowlist: ["https://github.com/example/repo"],
    mapping_rules_by_project_key: { TP: "https://github.com/example/repo" },
    mapping_rules_by_component: {},
    fallback_repo: null
  },
  policy: {
    allow_jira_transitions: false,
    allow_pr_creation: true,
    allow_label_mutations: true,
    max_runtime_minutes: 30,
    max_dev_test_review_loops: 2,
    max_concurrent_runs: 2,
    allowed_commands: [],
    require_agents_md: false
  },
  discord: null
};

function statusBadge(status: string) {
  if (status === "succeeded") return <Badge variant="default">{status}</Badge>;
  if (status === "failed" || status === "blocked") return <Badge variant="secondary">{status}</Badge>;
  return <Badge variant="outline">{status}</Badge>;
}

export default function AdminPage() {
  const [apiBaseUrl, setApiBaseUrl] = useState(process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8100");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const [tenants, setTenants] = useState<TenantRecord[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [runDetail, setRunDetail] = useState<RunRecord | null>(null);

  const [createJson, setCreateJson] = useState(JSON.stringify(tenantTemplate, null, 2));
  const [editTenantId, setEditTenantId] = useState("");
  const [editJson, setEditJson] = useState("");

  const [runTenantFilter, setRunTenantFilter] = useState("");
  const [runStatusFilter, setRunStatusFilter] = useState("");

  const [loadingTenants, setLoadingTenants] = useState(false);
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [statusLine, setStatusLine] = useState("Connect and load data.");

  const credentials = useMemo<Credentials>(() => ({ apiBaseUrl, username, password }), [apiBaseUrl, username, password]);

  async function loadTenantList() {
    setLoadingTenants(true);
    try {
      const payload = await listTenants(credentials);
      setTenants(payload);
      setStatusLine(`Loaded ${payload.length} tenant(s).`);
    } catch (error) {
      setStatusLine(`Failed loading tenants: ${(error as Error).message}`);
    } finally {
      setLoadingTenants(false);
    }
  }

  async function loadRunList() {
    setLoadingRuns(true);
    try {
      const payload = await listRuns(credentials, {
        tenantId: runTenantFilter || undefined,
        status: runStatusFilter || undefined
      });
      setRuns(payload);
      setStatusLine(`Loaded ${payload.length} run(s).`);
    } catch (error) {
      setStatusLine(`Failed loading runs: ${(error as Error).message}`);
    } finally {
      setLoadingRuns(false);
    }
  }

  async function handleCreateTenant() {
    try {
      const parsed = JSON.parse(createJson);
      const created = await createTenant(credentials, parsed);
      setStatusLine(`Created tenant ${created.tenant_id}.`);
      await loadTenantList();
    } catch (error) {
      setStatusLine(`Create failed: ${(error as Error).message}`);
    }
  }

  async function handleLoadTenantForEdit(tenantId: string) {
    try {
      const tenant = await getTenant(credentials, tenantId);
      setEditTenantId(tenant.tenant_id);
      setEditJson(
        JSON.stringify(
          {
            name: tenant.name,
            is_enabled: tenant.is_enabled,
            jira: tenant.jira,
            github: tenant.github,
            repos: tenant.repos,
            policy: tenant.policy,
            discord: tenant.discord
          },
          null,
          2
        )
      );
      setStatusLine(`Loaded tenant ${tenantId} into editor.`);
    } catch (error) {
      setStatusLine(`Failed loading tenant: ${(error as Error).message}`);
    }
  }

  async function handleUpdateTenant() {
    if (!editTenantId.trim()) {
      setStatusLine("Tenant ID is required for update.");
      return;
    }
    try {
      const parsed = JSON.parse(editJson);
      await updateTenant(credentials, editTenantId.trim(), parsed);
      setStatusLine(`Updated tenant ${editTenantId}.`);
      await loadTenantList();
    } catch (error) {
      setStatusLine(`Update failed: ${(error as Error).message}`);
    }
  }

  async function handleDeleteTenant(tenantId: string) {
    try {
      await deleteTenant(credentials, tenantId);
      setStatusLine(`Deleted tenant ${tenantId}.`);
      await loadTenantList();
    } catch (error) {
      setStatusLine(`Delete failed: ${(error as Error).message}`);
    }
  }

  async function handleTenantHealth(tenantId: string) {
    try {
      const jira = await testJira(credentials, tenantId);
      const github = await testGithub(credentials, tenantId);
      setStatusLine(
        `Health ${tenantId}: Jira=${jira.ok ? "ok" : "fail"} (${jira.details}); GitHub=${github.ok ? "ok" : "fail"} (${github.details})`
      );
    } catch (error) {
      setStatusLine(`Health check failed: ${(error as Error).message}`);
    }
  }

  async function handleLoadRun(runId: string) {
    try {
      const detail = await getRun(credentials, runId);
      setRunDetail(detail);
      setStatusLine(`Loaded run ${runId}.`);
    } catch (error) {
      setStatusLine(`Failed loading run detail: ${(error as Error).message}`);
    }
  }

  return (
    <main className="min-h-screen p-6 md:p-10">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <section className="rounded-xl border bg-card/90 p-5 shadow-sm backdrop-blur">
          <h1 className="text-2xl font-semibold tracking-tight">master-builder admin</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Next.js + shadcn dashboard for tenant operations and run observability.
          </p>
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            <Input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} placeholder="API base URL" />
            <Input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Admin username" />
            <Input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Admin password"
            />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button onClick={loadTenantList}>
              {loadingTenants ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Load tenants
            </Button>
            <Button variant="secondary" onClick={loadRunList}>
              {loadingRuns ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
              Load runs
            </Button>
          </div>
          <p className="mt-3 text-sm text-muted-foreground">{statusLine}</p>
        </section>

        <div className="grid gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Tenants</CardTitle>
              <CardDescription>List, health-check, and manage configured tenants.</CardDescription>
            </CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Tenant</TableHead>
                    <TableHead>Name</TableHead>
                    <TableHead>Enabled</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tenants.map((tenant) => (
                    <TableRow key={tenant.tenant_id}>
                      <TableCell className="font-medium">{tenant.tenant_id}</TableCell>
                      <TableCell>{tenant.name}</TableCell>
                      <TableCell>
                        {tenant.is_enabled ? (
                          <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
                            <CheckCircle2 className="h-3 w-3" /> true
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-xs text-rose-700">
                            <XCircle className="h-3 w-3" /> false
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          <Button size="sm" variant="outline" onClick={() => handleLoadTenantForEdit(tenant.tenant_id)}>
                            Edit
                          </Button>
                          <Button size="sm" variant="secondary" onClick={() => handleTenantHealth(tenant.tenant_id)}>
                            Health
                          </Button>
                          <Button size="sm" variant="ghost" onClick={() => handleDeleteTenant(tenant.tenant_id)}>
                            Delete
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Create tenant</CardTitle>
              <CardDescription>Submit a full tenant payload to the admin API.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <Textarea value={createJson} onChange={(event) => setCreateJson(event.target.value)} className="min-h-[360px] font-mono text-xs" />
              <Button onClick={handleCreateTenant}>Create tenant</Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Edit tenant</CardTitle>
              <CardDescription>Load a tenant, modify JSON, then submit update.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <Input value={editTenantId} onChange={(event) => setEditTenantId(event.target.value)} placeholder="tenant id" />
              <Textarea value={editJson} onChange={(event) => setEditJson(event.target.value)} className="min-h-[280px] font-mono text-xs" />
              <Button onClick={handleUpdateTenant}>Update tenant</Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Runs</CardTitle>
              <CardDescription>Filter runs and inspect run detail payloads.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid gap-2 md:grid-cols-2">
                <Input value={runTenantFilter} onChange={(event) => setRunTenantFilter(event.target.value)} placeholder="tenant filter" />
                <Input value={runStatusFilter} onChange={(event) => setRunStatusFilter(event.target.value)} placeholder="status filter" />
              </div>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Tenant</TableHead>
                    <TableHead>Issue</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((run) => (
                    <TableRow key={run.run_id}>
                      <TableCell className="font-medium">{run.run_id}</TableCell>
                      <TableCell>{run.tenant_id}</TableCell>
                      <TableCell>{run.issue_key}</TableCell>
                      <TableCell>{statusBadge(run.status)}</TableCell>
                      <TableCell>
                        <Button size="sm" variant="outline" onClick={() => handleLoadRun(run.run_id)}>
                          View
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>

              <div className="rounded-md border bg-muted/30 p-3">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Run detail</p>
                <pre className="max-h-[260px] overflow-auto whitespace-pre-wrap text-xs">
                  {JSON.stringify(runDetail, null, 2)}
                </pre>
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </main>
  );
}
