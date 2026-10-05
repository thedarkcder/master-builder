import Link from "next/link";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const LAST_UPDATED = "October 5, 2026";

export default function PrivacyPage() {
  return (
    <main className="mx-auto grid min-h-screen w-full max-w-3xl place-items-center px-4 py-8">
      <Card className="w-full">
        <CardHeader>
          <CardTitle>Privacy Policy</CardTitle>
          <CardDescription>Last updated: {LAST_UPDATED}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4 text-sm leading-6 text-muted-foreground">
          <section>
            <p className="font-medium text-foreground">Information we collect</p>
            <p>
              This admin UI stores session and tenant configuration data needed to operate the orchestrator. Connection
              metadata may include tenant IDs, project keys, repository mappings, and integration health signals.
            </p>
          </section>
          <section>
            <p className="font-medium text-foreground">How data is used</p>
            <p>
              Data is used only to run tenant automations, process webhooks, and surface run status through the admin
              dashboard.
            </p>
          </section>
          <section>
            <p className="font-medium text-foreground">Secrets and credentials</p>
            <p>
              Secrets are referenced via server-side environment variables and OAuth tokens are stored encrypted at
              rest. Do not enter raw secrets directly into tenant forms.
            </p>
          </section>
          <section>
            <p className="font-medium text-foreground">Contact</p>
            <p>
              For privacy or data removal requests, contact your workspace administrator.
            </p>
          </section>
          <p>
            <Link href="/login" className="text-primary underline-offset-2 hover:underline">
              Back to login
            </Link>
          </p>
        </CardContent>
      </Card>
    </main>
  );
}
