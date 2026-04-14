const DEFAULT_DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export function formatTimestamp(value: string | null | undefined, emptyLabel = "—"): string {
  if (!value) {
    return emptyLabel;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DEFAULT_DATE_TIME_FORMATTER.format(parsed);
}
