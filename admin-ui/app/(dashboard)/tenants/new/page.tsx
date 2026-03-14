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
  const query = params.toString();
  redirect(query ? `/tenants/new/basics?${query}` : "/tenants/new/basics");
}
