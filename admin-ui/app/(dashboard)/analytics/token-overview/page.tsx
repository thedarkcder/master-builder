import { redirect } from "next/navigation";

export default function TokenOverviewLegacyRedirectPage() {
  redirect("/tenants/select");
}
