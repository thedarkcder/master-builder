import { redirect } from "next/navigation";

export default function TokenCompareLegacyRedirectPage() {
  redirect("/tenants/select");
}
