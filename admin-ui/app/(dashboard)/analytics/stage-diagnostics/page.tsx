import { redirect } from "next/navigation";

export default function StageDiagnosticsLegacyRedirectPage() {
  redirect("/tenants/select");
}
