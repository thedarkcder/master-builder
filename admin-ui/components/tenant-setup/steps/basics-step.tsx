import { Input } from "@/components/ui/input";

type BasicsStepProps = {
  name: string;
  tenantIdPreview: string;
  onNameChange: (name: string) => void;
};

export function BasicsStep({ name, tenantIdPreview, onNameChange }: BasicsStepProps) {
  return (
    <div className="max-w-2xl space-y-5">
      <div className="space-y-2">
        <label htmlFor="tenant-name" className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">
          Workspace name
        </label>
        <Input
          id="tenant-name"
          value={name}
          onChange={(event) => onNameChange(event.target.value)}
          placeholder="example"
          className="h-14 rounded-2xl border-slate-200 bg-white text-base"
        />
      </div>

      <div className="rounded-2xl border border-slate-200 bg-slate-50/70 px-4 py-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-slate-500">Workspace ID</div>
            <div className="mt-1 font-mono text-sm text-slate-900">{tenantIdPreview || "Generated from the workspace name."}</div>
          </div>
          <p className="text-sm text-slate-600">Generated automatically from the workspace name.</p>
        </div>
      </div>
    </div>
  );
}
