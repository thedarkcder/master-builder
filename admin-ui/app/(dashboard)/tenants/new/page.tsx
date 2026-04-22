import { redirect } from "next/navigation";

type SearchParams = Record<string, string | string[] | undefined>;

export default async function NewTenantRootRedirect({
  searchParams
}: {
  searchParams: Promise<SearchParams>;
}) {
  const resolvedSearchParams = await searchParams;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(resolvedSearchParams)) {
    if (typeof value === "string") {
      params.set(key, value);
      continue;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        params.append(key, item);
      }
    }
  }
  if (!params.has("tenant_id") && !params.has("atlassian_connection_id") && !params.has("github_install") && !params.has("discord_install")) {
    params.set("fresh", "1");
  }
  const query = params.toString();
  let targetStep = "basics";
  if (params.get("discord_install") === "success") {
    targetStep = "discord";
  } else if (params.get("github_install") === "success") {
    targetStep = "github";
  } else if (params.has("atlassian_connection_id") || params.get("atlassian_oauth") === "success") {
    targetStep = "jira";
  }

  redirect(query ? `/tenants/new/${targetStep}?${query}` : `/tenants/new/${targetStep}`);
}
