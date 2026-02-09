"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { listTenants, type TenantRecord } from "@/lib/api";

export default function SelectTenantPage() {
  const { credentials, ready } = useAuth();
  const [tenants, setTenants] = useState<TenantRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [statusLine, setStatusLine] = useState("Load tenants to start.");

  async function loadTenants() {
    if (!credentials) {
      return;
    }
    setLoading(true);
    try {
      const payload = await listTenants(credentials);
      setTenants(payload);
      setStatusLine(`Loaded ${payload.length} tenant(s).`);
    } catch (error) {
      setStatusLine(`Failed to load tenants: ${(error as Error).message}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (ready && credentials) {
      void loadTenants();
    }
  }, [ready, credentials]);

  const hasError = statusLine.toLowerCase().includes("failed");

  return (
    <div className="min-h-screen px-4 py-10">
      <div className="mx-auto w-full max-w-2xl">
        <Card>
          <CardHeader className="space-y-3">
            <p className="text-xs font-medium uppercase tracking-[0.16em] text-muted-foreground">Step 1 of 2</p>
            <div className="space-y-1">
              <CardTitle className="text-2xl">Select Tenant</CardTitle>
              <CardDescription>Choose an account to open its admin workspace.</CardDescription>
            </div>
            <p className={hasError ? "text-sm text-red-700" : "text-sm text-muted-foreground"}>{statusLine}</p>
          </CardHeader>
          <CardContent className="space-y-3">
            {loading ? (
              <div className="space-y-3">
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
                <Skeleton className="h-20 w-full" />
              </div>
            ) : tenants.length === 0 ? (
              <div className="space-y-3">
                <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
                  No tenants found yet.
                </p>
                <Button asChild>
                  <Link href="/tenants/new">Create your first tenant</Link>
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                {tenants.map((tenant) => (
                  <Link
                    key={tenant.tenant_id}
                    href={`/tenants/${encodeURIComponent(tenant.tenant_id)}/edit/integrations`}
                    className="block rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Card className="cursor-pointer transition hover:border-primary hover:shadow-sm">
                      <CardContent className="flex items-center justify-between py-4">
                        <div>
                          <p className="font-semibold">{tenant.name}</p>
                          <p className="text-sm text-muted-foreground">{tenant.tenant_id}</p>
                        </div>
                        <ChevronRight className="h-4 w-4 text-muted-foreground" />
                      </CardContent>
                    </Card>
                  </Link>
                ))}
                <Link
                  href="/tenants/new"
                  className="block rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Card className="cursor-pointer border-dashed transition hover:border-primary hover:shadow-sm">
                    <CardContent className="flex items-center justify-between py-4">
                      <div>
                        <p className="font-semibold">Create New Tenant</p>
                        <p className="text-sm text-muted-foreground">Add another account workspace.</p>
                      </div>
                      <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    </CardContent>
                  </Card>
                </Link>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
