"use client";

import { useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";

import {
  defaultTenantFormValues,
  formValuesToTextFields,
  type TenantFormTextFields,
  type TenantFormValues
} from "@/lib/tenant-form";
import {
  resolveTenantFormSections,
  validateTenantForm,
  type TenantFormSections,
  type TenantFormVisibleSections
} from "@/components/tenant-form-config";

type UseTenantFormStateArgs = {
  initialValues?: TenantFormValues;
  visibleSections?: TenantFormVisibleSections;
};

export type TenantFormState = {
  values: TenantFormValues;
  setValues: Dispatch<SetStateAction<TenantFormValues>>;
  textFields: TenantFormTextFields;
  setTextFields: Dispatch<SetStateAction<TenantFormTextFields>>;
  sections: TenantFormSections;
  validation: string;
  error: string;
  setError: Dispatch<SetStateAction<string>>;
};

export function useTenantFormState({ initialValues, visibleSections }: UseTenantFormStateArgs): TenantFormState {
  const [values, setValues] = useState<TenantFormValues>(initialValues ?? defaultTenantFormValues());
  const [textFields, setTextFields] = useState(() => formValuesToTextFields(initialValues ?? defaultTenantFormValues()));
  const [error, setError] = useState("");

  useEffect(() => {
    const nextValues = initialValues ?? defaultTenantFormValues();
    setValues(nextValues);
    setTextFields(formValuesToTextFields(nextValues));
  }, [initialValues]);

  const sections = useMemo(() => resolveTenantFormSections(visibleSections), [visibleSections]);
  const validation = useMemo(
    () => validateTenantForm({ sections, values, textFields }),
    [sections, textFields, values]
  );

  return {
    values,
    setValues,
    textFields,
    setTextFields,
    sections,
    validation,
    error,
    setError
  };
}
