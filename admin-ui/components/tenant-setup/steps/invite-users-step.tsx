import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type InviteRole = "tenant_admin" | "technical_member" | "business_member";

type InviteUsersStepProps = {
  createdTenantId: string;
  tenantIdPreview: string;
  inviteEmail: string;
  inviteFullName: string;
  inviteRole: InviteRole;
  sendingInvite: boolean;
  recentInvites: Array<{
    invite_id: string;
    email: string;
    role: string;
    status: string;
  }>;
  onInviteEmailChange: (value: string) => void;
  onInviteFullNameChange: (value: string) => void;
  onInviteRoleChange: (value: InviteRole) => void;
  onSendInvite: () => void;
};

function roleLabel(role: string): string {
  if (role === "tenant_admin") {
    return "Admin";
  }
  if (role === "technical_member") {
    return "Technical";
  }
  return "Business";
}

export function InviteUsersStep({
  createdTenantId,
  tenantIdPreview,
  inviteEmail,
  inviteFullName,
  inviteRole,
  sendingInvite,
  recentInvites,
  onInviteEmailChange,
  onInviteFullNameChange,
  onInviteRoleChange,
  onSendInvite
}: InviteUsersStepProps) {
  return (
    <div className="grid gap-6 xl:grid-cols-[360px_minmax(0,1fr)]">
      <div className="space-y-4 rounded-2xl border border-slate-200 bg-slate-50/70 px-5 py-5">
        <div className="text-sm text-slate-600">
          Workspace: <strong className="text-slate-900">{createdTenantId || tenantIdPreview}</strong>
        </div>
        <div className="space-y-3">
          <Input
            placeholder="Email address"
            value={inviteEmail}
            onChange={(event) => onInviteEmailChange(event.target.value)}
            className="h-11 rounded-xl border-slate-200 bg-white"
          />
          <Input
            placeholder="Full name (optional)"
            value={inviteFullName}
            onChange={(event) => onInviteFullNameChange(event.target.value)}
            className="h-11 rounded-xl border-slate-200 bg-white"
          />
          <select
            value={inviteRole}
            onChange={(event) => onInviteRoleChange(event.target.value as InviteRole)}
            className="h-11 w-full rounded-xl border border-slate-200 bg-white px-3 text-sm text-slate-900"
          >
            <option value="business_member">Business member</option>
            <option value="technical_member">Technical member</option>
            <option value="tenant_admin">Tenant admin</option>
          </select>
          <Button onClick={onSendInvite} disabled={sendingInvite || !inviteEmail.trim()} className="w-full">
            {sendingInvite ? "Sending invite..." : "Send invite"}
          </Button>
        </div>
      </div>

      <div className="rounded-2xl border border-slate-200 bg-white px-5 py-5">
        <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Sent invites</div>
        <div className="mt-3 space-y-2">
          {recentInvites.length === 0 ? (
            <p className="text-sm text-slate-500">No invites sent yet.</p>
          ) : (
            recentInvites.map((invite) => (
              <div key={invite.invite_id} className="rounded-xl border border-slate-200 px-3 py-2 text-sm">
                <div className="font-medium text-slate-900">{invite.email}</div>
                <div className="text-xs text-slate-500">
                  {roleLabel(invite.role)} · {invite.status}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
