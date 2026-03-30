"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { CodexModelOptionRecord } from "@/lib/api";
import { Input } from "@/components/ui/input";

const CUSTOM_MODEL_VALUE = "__custom__";

type Props = {
  value: string | null | undefined;
  models: CodexModelOptionRecord[];
  inheritLabel: string;
  ariaLabel?: string;
  helperText?: string;
  effectiveLabel?: string;
  disabled?: boolean;
  /** When this changes (e.g. profile/tenant being edited), exit explicit custom mode. */
  editorSurfaceKey?: string | null;
  onChange: (value: string | null) => void;
};

export function CodexModelSelect({
  value,
  models,
  inheritLabel,
  ariaLabel,
  helperText,
  effectiveLabel,
  disabled = false,
  editorSurfaceKey,
  onChange,
}: Props) {
  const normalizedValue = String(value || "").trim();
  const optionIds = useMemo(
    () => new Set(models.map((option) => String(option.id || "").trim()).filter(Boolean)),
    [models],
  );
  const valueIsPreset = normalizedValue.length > 0 && optionIds.has(normalizedValue);
  /** User chose "Custom model…"; stay in custom UI even if the id matches a catalog preset. */
  const customExplicitRef = useRef(false);
  const [customMode, setCustomMode] = useState<boolean>(normalizedValue.length > 0 && !valueIsPreset);
  const [customValue, setCustomValue] = useState<string>(valueIsPreset ? "" : normalizedValue);

  useEffect(() => {
    customExplicitRef.current = false;
  }, [editorSurfaceKey]);

  useEffect(() => {
    const nextNormalized = String(value ?? "").trim();
    const nextPreset = nextNormalized.length > 0 && optionIds.has(nextNormalized);

    if (customExplicitRef.current) {
      setCustomMode(true);
      setCustomValue(nextNormalized);
      return;
    }

    if (nextPreset) {
      setCustomMode(false);
      setCustomValue("");
      return;
    }
    if (nextNormalized.length > 0) {
      setCustomMode(true);
      setCustomValue(nextNormalized);
      return;
    }
    setCustomMode(false);
    setCustomValue("");
  }, [optionIds, value, editorSurfaceKey]);

  const selectValue = customMode ? CUSTOM_MODEL_VALUE : normalizedValue;

  return (
    <div className="space-y-2">
      <select
        aria-label={ariaLabel}
        className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
        value={selectValue}
        onChange={(event) => {
          const next = event.target.value;
          if (next === CUSTOM_MODEL_VALUE) {
            customExplicitRef.current = true;
            setCustomMode(true);
            setCustomValue("");
            onChange(null);
            return;
          }
          customExplicitRef.current = false;
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
          aria-label={ariaLabel ? `${ariaLabel} custom value` : undefined}
          value={customValue}
          onChange={(event) => {
            const next = event.target.value;
            setCustomValue(next);
            onChange(next.trim() || null);
          }}
          placeholder="Enter model id"
          disabled={disabled}
        />
      ) : null}
      {effectiveLabel ? <p className="text-xs text-muted-foreground">{effectiveLabel}</p> : null}
      {helperText ? <p className="text-xs text-muted-foreground">{helperText}</p> : null}
    </div>
  );
}
