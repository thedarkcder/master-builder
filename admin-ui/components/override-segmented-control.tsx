"use client";

import { cn } from "@/lib/utils";

type OverrideSegmentOption<T extends string> = {
  value: T;
  label: string;
};

type OverrideSegmentedControlProps<T extends string> = {
  value: T;
  options: OverrideSegmentOption<T>[];
  disabled?: boolean;
  onChange: (value: T) => void;
};

export function OverrideSegmentedControl<T extends string>({
  value,
  options,
  disabled = false,
  onChange,
}: OverrideSegmentedControlProps<T>) {
  return (
    <div className="inline-flex w-full rounded-lg border border-input bg-muted/30 p-1">
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            className={cn(
              "flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              selected
                ? "bg-background text-foreground shadow-sm"
                : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
            )}
            disabled={disabled}
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
