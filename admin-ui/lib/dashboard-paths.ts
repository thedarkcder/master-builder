export type ProjectSection = "overview" | "settings" | "runs" | "webhooks" | "notifications" | "automations" | "secrets" | "danger";
export type RunPanel = "overview" | "agents" | "diagnostics" | "cost";

function encodeSegment(value: string): string {
  return encodeURIComponent(value);
}

function decodeSegment(value: string | undefined): string | null {
  if (!value) {
    return null;
  }
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function buildProjectSectionPath(
  tenantId: string,
  projectId: string,
  section: ProjectSection = "overview",
): string {
  const base = `/${encodeSegment(tenantId)}/projects/${encodeSegment(projectId)}`;
  if (section === "overview") {
    return base;
  }
  return `${base}/${section}`;
}

export function buildRunDetailPath({
  tenantId,
  projectId,
  runId,
  panel = "overview",
}: {
  tenantId?: string | null;
  projectId?: string | null;
  runId: string;
  panel?: RunPanel;
}): string {
  const encodedRunId = encodeSegment(runId);
  let base = `/runs/${encodedRunId}`;
  if (tenantId && projectId) {
    base = `/${encodeSegment(tenantId)}/projects/${encodeSegment(projectId)}/runs/${encodedRunId}`;
  } else if (tenantId) {
    base = `/${encodeSegment(tenantId)}/runs/${encodedRunId}`;
  }
  if (panel === "overview") {
    return base;
  }
  return `${base}/${panel}`;
}

export function resolveProjectSection(pathname: string): ProjectSection | null {
  const match = pathname.match(/^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))[^/]+\/projects\/[^/]+(?:\/([^/]+))?(?:\/|$)/);
  const rawSection = match?.[1] ?? "";
  if (!rawSection) {
    return "overview";
  }
  if (
    rawSection === "settings" ||
    rawSection === "runs" ||
    rawSection === "webhooks" ||
    rawSection === "notifications" ||
    rawSection === "automations" ||
    rawSection === "secrets" ||
    rawSection === "danger"
  ) {
    return rawSection;
  }
  if (rawSection === "discord") {
    return "notifications";
  }
  return null;
}

export function resolveRunRouteContext(pathname: string): {
  tenantId: string | null;
  projectId: string | null;
  runId: string | null;
  panel: RunPanel;
} {
  const projectMatch = pathname.match(
    /^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))([^/]+)\/projects\/([^/]+)\/runs\/([^/]+)(?:\/(agents|diagnostics|cost))?(?:\/|$)/,
  );
  if (projectMatch) {
    return {
      tenantId: decodeSegment(projectMatch[1]),
      projectId: decodeSegment(projectMatch[2]),
      runId: decodeSegment(projectMatch[3]),
      panel: (projectMatch[4] as RunPanel | undefined) ?? "overview",
    };
  }
  const tenantMatch = pathname.match(/^\/(?!tenants(?:\/|$)|runs(?:\/|$)|platform(?:\/|$)|login(?:\/|$)|register(?:\/|$)|invite(?:\/|$)|get-started(?:\/|$)|forgot-password(?:\/|$)|reset-password(?:\/|$)|api(?:\/|$))([^/]+)\/runs\/([^/]+)(?:\/(agents|diagnostics|cost))?(?:\/|$)/);
  if (tenantMatch) {
    return {
      tenantId: decodeSegment(tenantMatch[1]),
      projectId: null,
      runId: decodeSegment(tenantMatch[2]),
      panel: (tenantMatch[3] as RunPanel | undefined) ?? "overview",
    };
  }
  const globalMatch = pathname.match(/^\/runs\/([^/]+)(?:\/(agents|diagnostics|cost))?(?:\/|$)/);
  if (globalMatch) {
    return {
      tenantId: null,
      projectId: null,
      runId: decodeSegment(globalMatch[1]),
      panel: (globalMatch[2] as RunPanel | undefined) ?? "overview",
    };
  }
  return {
    tenantId: null,
    projectId: null,
    runId: null,
    panel: "overview",
  };
}
