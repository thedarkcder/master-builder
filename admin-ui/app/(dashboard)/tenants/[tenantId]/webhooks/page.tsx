"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft, Check, Copy } from "lucide-react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type WebhookDoc = {
  key: string;
  name: string;
  description: string;
  url: string;
};

export default function TenantWebhooksPage() {
  const { credentials } = useAuth();
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState("Copy webhook URLs for this tenant.");

  const apiBaseUrl = credentials?.apiBaseUrl?.replace(/\/$/, "") ?? "http://localhost:4000";
  const docs: WebhookDoc[] = [
    {
      key: "jira",
      name: "Jira Webhook URL",
      description: "Configure Jira issue webhooks for this tenant to trigger runs.",
      url: `${apiBaseUrl}/jira/webhook/${tenantId}`
    },
    {
      key: "github",
      name: "GitHub Webhook URL",
      description: "Configure your GitHub App or repository webhook for PR/check events.",
      url: `${apiBaseUrl}/github/webhook`
    },
    {
      key: "discord",
      name: "Discord Interactions URL",
      description: "Set this in Discord Developer Portal > Interactions Endpoint URL.",
      url: `${apiBaseUrl}/discord/interactions`
    }
  ];

  async function copyWebhook(key: string, url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setCopiedKey(key);
      setStatusLine(`Copied: ${url}`);
      window.setTimeout(() => {
        setCopiedKey((current) => (current === key ? null : current));
      }, 1200);
    } catch (error) {
      setStatusLine(`Copy failed: ${(error as Error).message}`);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle>Tenant Webhooks: {tenantId}</CardTitle>
            <CardDescription>Webhook endpoints for tenant-specific integrations.</CardDescription>
          </div>
          <Button variant="outline" asChild>
            <Link href={`/tenants/${encodeURIComponent(tenantId)}/edit`}>
              <ArrowLeft className="mr-2 h-4 w-4" />
              Back To Tenant
            </Link>
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="rounded-md border bg-muted/30 px-3 py-2 text-sm text-foreground">{statusLine}</p>
        <div className="space-y-3">
          {docs.map((doc) => (
            <div
              key={doc.key}
              className="grid gap-2 rounded-md border p-3 md:grid-cols-[220px_1fr_auto] md:items-center"
            >
              <div>
                <p className="text-sm font-medium">{doc.name}</p>
                <p className="text-xs text-muted-foreground">{doc.description}</p>
              </div>
              <code className="block overflow-x-auto rounded bg-muted/60 px-2 py-1 text-xs">{doc.url}</code>
              <Button type="button" variant="outline" size="sm" onClick={() => void copyWebhook(doc.key, doc.url)}>
                {copiedKey === doc.key ? (
                  <>
                    <Check className="mr-2 h-4 w-4" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="mr-2 h-4 w-4" />
                    Copy
                  </>
                )}
              </Button>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
