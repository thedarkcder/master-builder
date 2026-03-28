import { ProjectKnowledgeBrowserPage } from "@/components/project-knowledge-browser-page";

type KnowledgePageParams = {
  tenantId: string;
  projectId: string;
};

export default async function ProjectKnowledgePage({
  params,
  searchParams
}: {
  params: Promise<KnowledgePageParams>;
  searchParams: Promise<{ view?: string }>;
}) {
  const resolvedParams = await params;
  const resolvedSearchParams = await searchParams;
  return (
    <ProjectKnowledgeBrowserPage
      tenantId={resolvedParams.tenantId}
      projectId={resolvedParams.projectId}
      initialView={
        resolvedSearchParams.view === "add"
          ? "add"
          : resolvedSearchParams.view === "sources"
            ? "sources"
            : "browse"
      }
    />
  );
}
