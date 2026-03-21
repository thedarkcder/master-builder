type SearchParamSource = {
  get(name: string): string | null;
  toString(): string;
};

export type QueryValue = boolean | number | string | readonly string[] | null | undefined;

export function readQueryString(searchParams: SearchParamSource, key: string, fallback = ""): string {
  const value = searchParams.get(key);
  return value === null ? fallback : value;
}

export function readQueryNumber(searchParams: SearchParamSource, key: string, fallback: number): number {
  const raw = searchParams.get(key);
  if (raw === null || raw.trim() === "") {
    return fallback;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function readQueryBoolean(searchParams: SearchParamSource, key: string, fallback: boolean): boolean {
  const raw = searchParams.get(key);
  if (raw === null || raw.trim() === "") {
    return fallback;
  }
  if (raw === "true") {
    return true;
  }
  if (raw === "false") {
    return false;
  }
  return fallback;
}

export function readQueryArray(searchParams: SearchParamSource, key: string): string[] {
  const raw = searchParams.get(key);
  if (raw === null || raw.trim() === "") {
    return [];
  }
  return raw
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
}

function serializeQueryValue(value: QueryValue): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (Array.isArray(value)) {
    return [...new Set(value.map((entry) => String(entry).trim()).filter(Boolean))].sort().join(",");
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  return String(value);
}

export function buildQueryString(
  searchParams: SearchParamSource,
  entries: Record<string, QueryValue>,
): string {
  const next = new URLSearchParams(searchParams.toString());
  for (const [key, value] of Object.entries(entries)) {
    const serialized = serializeQueryValue(value);
    if (serialized === null) {
      next.delete(key);
      continue;
    }
    next.set(key, serialized);
  }
  return next.toString();
}

export function buildUrlWithQuery(
  pathname: string,
  searchParams: SearchParamSource,
  entries: Record<string, QueryValue>,
): string {
  const query = buildQueryString(searchParams, entries);
  return `${pathname}${query ? `?${query}` : ""}`;
}
