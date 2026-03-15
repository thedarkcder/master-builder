import { STEP_ORDER } from "@/components/tenant-setup/types";

export function WizardProgress({ stepIndex }: { stepIndex: number }) {
  return (
    <ol className="grid gap-2 md:grid-cols-3">
      {STEP_ORDER.map((step, index) => {
        const active = index === stepIndex;
        const complete = index < stepIndex;
        return (
          <li
            key={step.key}
            className={`rounded-md border px-3 py-2 text-xs ${
              active ? "border-primary bg-primary/10 text-primary" : complete ? "border-emerald-300 bg-emerald-50" : ""
            }`}
          >
            <span className="font-semibold">{index + 1}.</span> {step.label}
          </li>
        );
      })}
    </ol>
  );
}
