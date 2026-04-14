"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

const tabs = [
  { label: "Integrations", section: "integrations" },
  { label: "Jira", section: "jira" },
  { label: "GitHub", section: "github" },
  { label: "Discord", section: "discord" },
  { label: "Configuration", section: "config" },
  { label: "Health", section: "health" },
  { label: "Notifications", section: "notifications" },
  { label: "Danger", section: "danger" },
];

export default function SettingsLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ tenantId: string }>();
  const pathname = usePathname();
  const tenantId = decodeURIComponent(params.tenantId);

  return (
    <div className="space-y-0">
      <div className="overflow-x-auto border-b">
        <nav className="-mb-px flex min-w-max gap-0" aria-label="Settings tabs">
          {tabs.map((tab) => {
            const href = `/${encodeURIComponent(tenantId)}/settings/${tab.section}`;
            const active = pathname === href || pathname.startsWith(`${href}/`);
            const isDanger = tab.section === "danger";
            return (
              <Link
                key={tab.section}
                href={href}
                className={[
                  "inline-flex items-center border-b-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
                  isDanger && active
                    ? "border-red-500 text-red-600"
                    : isDanger
                      ? "border-transparent text-red-400 hover:border-red-300 hover:text-red-500"
                      : active
                        ? "border-primary text-foreground"
                        : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
                ].join(" ")}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </div>
      <div className="pt-6">{children}</div>
    </div>
  );
}
