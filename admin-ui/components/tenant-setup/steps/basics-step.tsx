import { Input } from "@/components/ui/input";

type BasicsStepProps = {
  name: string;
  tenantIdPreview: string;
  onNameChange: (name: string) => void;
};

export function BasicsStep({ name, tenantIdPreview, onNameChange }: BasicsStepProps) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      <div className="space-y-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant Name</label>
        <Input value={name} onChange={(event) => onNameChange(event.target.value)} placeholder="Tenant Demo" />
      </div>
      <div className="space-y-2">
        <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Tenant ID Preview</label>
        <Input value={tenantIdPreview} disabled />
      </div>
    </div>
  );
}
