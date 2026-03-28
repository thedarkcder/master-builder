import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";

export default async function HomePage() {
  const session = await auth();
  redirect(getDefaultAuthenticatedRoute(session?.user?.principal));
}
