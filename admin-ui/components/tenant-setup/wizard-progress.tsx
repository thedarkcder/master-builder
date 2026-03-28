import { STEP_ORDER } from "@/components/tenant-setup/types";

export function WizardProgress({ stepIndex }: { stepIndex: number }) {
  return (
    <ol className="space-y-0.5">
      {STEP_ORDER.map((step, index) => {
        const active = index === stepIndex;
        const complete = index < stepIndex;
        return (
          <li key={step.key} className="relative pl-11">
            {index < STEP_ORDER.length - 1 ? (
              <span
                className={[
                  "absolute left-[12px] top-8 bottom-[-12px] w-px",
                  complete ? "bg-emerald-300" : "bg-slate-200",
                ].join(" ")}
              />
            ) : null}
            <div className="flex items-start gap-3 py-1.5">
              <span
                className={[
                  "absolute left-0 top-1.5 inline-flex h-6 w-6 items-center justify-center rounded-full text-[11px] font-semibold transition-colors",
                  active
                    ? "bg-slate-950 text-white"
                    : complete
                      ? "bg-emerald-100 text-emerald-800"
                      : "bg-white text-slate-500 ring-1 ring-slate-200",
                ].join(" ")}
              >
                {index + 1}
              </span>
              <div className="min-w-0">
                <div
                  className={[
                    "text-[11px] font-semibold uppercase tracking-[0.18em]",
                    active ? "text-slate-950" : complete ? "text-emerald-700" : "text-slate-400",
                  ].join(" ")}
                >
                  {step.label}
                </div>
                <div
                  className={[
                    "text-sm leading-5",
                    active ? "font-semibold text-slate-950" : complete ? "text-slate-700" : "text-slate-500",
                  ].join(" ")}
                >
                  {step.title}
                </div>
              </div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
