const DEFAULT_DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const DEFAULT_RELATIVE_TIME_FORMATTER = new Intl.RelativeTimeFormat(undefined, {
  numeric: "auto",
});

export function formatTimestamp(value: string | null | undefined, emptyLabel = "—"): string {
  if (!value) {
    return emptyLabel;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : DEFAULT_DATE_TIME_FORMATTER.format(parsed);
}

export function formatTimeAgo(value: string | null | undefined, emptyLabel = "—"): string {
  if (!value) {
    return emptyLabel;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  const deltaSeconds = Math.round((parsed.getTime() - Date.now()) / 1000);
  const absSeconds = Math.abs(deltaSeconds);

  if (absSeconds < 60) {
    return DEFAULT_RELATIVE_TIME_FORMATTER.format(Math.trunc(deltaSeconds), "second");
  }

  const deltaMinutes = Math.round(deltaSeconds / 60);
  if (Math.abs(deltaMinutes) < 60) {
    return DEFAULT_RELATIVE_TIME_FORMATTER.format(deltaMinutes, "minute");
  }

  const deltaHours = Math.round(deltaMinutes / 60);
  if (Math.abs(deltaHours) < 24) {
    return DEFAULT_RELATIVE_TIME_FORMATTER.format(deltaHours, "hour");
  }

  const deltaDays = Math.round(deltaHours / 24);
  return DEFAULT_RELATIVE_TIME_FORMATTER.format(deltaDays, "day");
}
