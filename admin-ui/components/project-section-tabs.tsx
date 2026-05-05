"use client";

import Link from "next/link";

import { buildProjectSectionPath, type ProjectSection } from "@/lib/dashboard-paths";

type ProjectSectionTab = {
  id: ProjectSection;
  label: string;
  visibility: "all" | "manager" | "platform";
};

const PROJECT_SECTION_TABS: ProjectSectionTab[] = [
  { id: "overview", label: "Overview", visibility: "all" },
  { id: "knowledge", label: "Knowledge", visibility: "all" },
  { id: "settings", label: "Settings", visibility: "manager" },
  { id: "runs", label: "Runs", visibility: "platform" },
  { id: "webhooks", label: "Webhooks", visibility: "manager" },
  { id: "notifications", label: "Notifications", visibility: "manager" },
  { id: "automations", label: "Automations", visibility: "manager" },
  { id: "secrets", label: "Secrets", visibility: "manager" },
  { id: "danger", label: "Danger", visibility: "manager" },
];

type ProjectSectionTabsProps = {
  tenantId: string;
  projectId: string;
  activeSection: ProjectSection;
  allowProjectManagement: boolean;
  isPlatformSuperAdmin: boolean;
  notificationCount?: number;
};

export function ProjectSectionTabs({
  tenantId,
  projectId,
  activeSection,
  allowProjectManagement,
  isPlatformSuperAdmin,
  notificationCount = 0,
}: ProjectSectionTabsProps) {
  const visibleTabs = PROJECT_SECTION_TABS.filter((tab) => {
    if (tab.visibility === "platform") {
      return isPlatformSuperAdmin;
    }
    if (tab.visibility === "manager") {
      return allowProjectManagement;
    }
    return true;
  });

  return (
    <div className="overflow-x-auto border-b">
      <nav className="-mb-px flex min-w-max gap-1" aria-label="Project sections">
        {visibleTabs.map((tab) => {
          const isDanger = tab.id === "danger";
          return (
            <Link
              key={tab.id}
              href={buildProjectSectionPath(tenantId, projectId, tab.id)}
              className={[
                "whitespace-nowrap border-b-2 px-4 py-2.5 text-sm font-medium transition-colors",
                isDanger && activeSection === tab.id
                  ? "border-red-500 text-red-600"
                  : isDanger
                    ? "border-transparent text-red-400 hover:border-red-300 hover:text-red-500"
                    : activeSection === tab.id
                      ? "border-primary text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground",
              ].join(" ")}
            >
              <span>{tab.label}</span>
              {tab.id === "notifications" && notificationCount > 0 ? (
                <span className="ml-2 inline-flex min-w-5 items-center justify-center rounded-full bg-warning px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">
                  {notificationCount}
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
