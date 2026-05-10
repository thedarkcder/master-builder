import { redirect } from "next/navigation";

type StartWorkRedirectPageProps = {
  params: Promise<{ tenantId: string; executionId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

export default async function StartWorkRedirectPage({ params, searchParams }: StartWorkRedirectPageProps) {
  const { tenantId, executionId } = await params;
  const resolvedSearchParams = await searchParams;
  const query = new URLSearchParams();

  for (const [key, value] of Object.entries(resolvedSearchParams)) {
    if (Array.isArray(value)) {
      for (const item of value) {
        query.append(key, item);
      }
    } else if (typeof value === "string") {
      query.set(key, value);
    }
  }

  const suffix = query.toString() ? `?${query.toString()}` : "";
  redirect(
    `/${encodeURIComponent(tenantId)}/start-engineering/${encodeURIComponent(executionId)}${suffix}`,
  );
}
