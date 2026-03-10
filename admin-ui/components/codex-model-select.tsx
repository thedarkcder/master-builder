"use client";

import { useEffect, useMemo, useState } from "react";

import type { CodexModelOptionRecord } from "@/lib/api";
import { Input } from "@/components/ui/input";

const CUSTOM_MODEL_VALUE = "__custom__";

type Props = {
  value: string | null | undefined;
  models: CodexModelOptionRecord[];
  inheritLabel: string;
  helperText?: string;
  effectiveLabel?: string;
  disabled?: boolean;
  onChange: (value: string | null) => void;
};

export function CodexModelSelect({
  value,
  models,
  inheritLabel,
  helperText,
  effectiveLabel,
  disabled = false,
  onChange,
}: Props) {
  const normalizedValue = String(value || "").trim();
  const optionIds = useMemo(
    () => new Set(models.map((option) => String(option.id || "").trim()).filter(Boolean)),
    [models],
  );
  const valueIsPreset = normalizedValue.length > 0 && optionIds.has(normalizedValue);
  const [customMode, setCustomMode] = useState<boolean>(normalizedValue.length > 0 && !valueIsPreset);
  const [customValue, setCustomValue] = useState<string>(valueIsPreset ? "" : normalizedValue);

  useEffect(() => {
    const nextNormalized = String(value || "").trim();
    const nextPreset = nextNormalized.length > 0 && optionIds.has(nextNormalized);
    setCustomMode(nextNormalized.length > 0 && !nextPreset);
    setCustomValue(nextPreset ? "" : nextNormalized);
  }, [optionIds, value]);

  const selectValue = customMode ? CUSTOM_MODEL_VALUE : normalizedValue;

  return (
    <div className="space-y-2">
      <select
        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
        value={selectValue}
        onChange={(event) => {
          const next = event.target.value;
          if (next === CUSTOM_MODEL_VALUE) {
            setCustomMode(true);
            setCustomValue("");
            onChange(null);
            return;
          }
          setCustomMode(false);
          setCustomValue("");
          onChange(next.trim() || null);
        }}
        disabled={disabled}
      >
        <option value="">{inheritLabel}</option>
        {models.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
        <option value={CUSTOM_MODEL_VALUE}>Custom model…</option>
      </select>
      {customMode ? (
        <Input
          value={customValue}
          onChange={(event) => {
            const next = event.target.value;
            setCustomValue(next);
            onChange(next.trim() || null);
          }}
          placeholder="Enter Codex model id"
          disabled={disabled}
        />
      ) : null}
      {effectiveLabel ? <p className="text-xs text-muted-foreground">{effectiveLabel}</p> : null}
      {helperText ? <p className="text-xs text-muted-foreground">{helperText}</p> : null}
    </div>
  );
}
