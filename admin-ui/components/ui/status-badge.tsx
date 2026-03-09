import { Badge } from "@/components/ui/badge";

type StatusBadgeProps = {
  status: string;
  className?: string;
};

export function StatusBadge({ status, className }: StatusBadgeProps) {
  const normalized = status?.toLowerCase().trim() ?? "";

  if (normalized === "succeeded") {
    return <Badge variant="success" className={className}>{status}</Badge>;
  }
  if (normalized === "failed") {
    return <Badge variant="destructive" className={className}>{status}</Badge>;
  }
  if (normalized === "blocked") {
    return <Badge variant="destructive" className={className}>{status}</Badge>;
  }
  if (normalized === "running") {
    return <Badge variant="warning" className={className}>{status}</Badge>;
  }
  if (normalized === "queued") {
    return <Badge variant="info" className={className}>{status}</Badge>;
  }
  return <Badge variant="outline" className={className}>{status}</Badge>;
}
