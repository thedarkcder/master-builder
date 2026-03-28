import { Suspense } from "react";

import { AcceptInviteClient } from "./accept-invite-client";

function AcceptInviteFallback() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 py-8">
      <p className="text-sm text-slate-400">Loading…</p>
    </main>
  );
}

export default function AcceptInvitePage() {
  return (
    <Suspense fallback={<AcceptInviteFallback />}>
      <AcceptInviteClient />
    </Suspense>
  );
}
