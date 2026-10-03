import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import type { Session } from "next-auth";

import { auth } from "@/auth";
import type { AuthenticatedPrincipalRecord } from "@/lib/api";
import { getDefaultAuthenticatedRoute } from "@/lib/auth-routing";
import { getLastWorkspaceCookieName } from "@/lib/workspace-preference";

function defaultRouteForSession(session: Session | null, preferredTenantId?: string | null): string {
  return getDefaultAuthenticatedRoute(
    ((session?.user ?? {}) as { principal?: AuthenticatedPrincipalRecord }).principal,
    { preferredTenantId },
  );
}

export default auth((request: NextRequest & { auth: Session | null }) => {
  const { pathname } = request.nextUrl;
  const pathSegments = pathname.split("/").filter(Boolean);
  const tenantScopedSurface = pathSegments[1] ?? null;
  const isPublicPath =
    pathname === "/" ||
    pathname === "/login" ||
    pathname === "/register" ||
    pathname === "/forgot-password" ||
    pathname === "/reset-password" ||
    pathname === "/privacy" ||
    pathname === "/licenses/manrope-OFL.txt" ||
    pathname === "/licenses/manrope-NOTICE.txt" ||
    pathname === "/blog" ||
    pathname.startsWith("/blog/") ||
    pathname.startsWith("/invite/accept") ||
    tenantScopedSurface === "start" ||
    tenantScopedSurface === "start-engineering";
  const session = request.auth;
  const preferredTenantId = request.cookies.get(getLastWorkspaceCookieName())?.value ?? null;
  const principal = ((session?.user ?? {}) as { principal?: AuthenticatedPrincipalRecord }).principal;

  if (pathname.startsWith("/_next") || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  if (!session && !isPublicPath) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", `${pathname}${request.nextUrl.search}`);
    return NextResponse.redirect(loginUrl);
  }

  if (session && (pathname === "/login" || pathname === "/register")) {
    const nextPath = request.nextUrl.searchParams.get("next");
    if (nextPath?.startsWith("/") && !nextPath.startsWith("//")) {
      return NextResponse.redirect(new URL(nextPath, request.url));
    }
    return NextResponse.redirect(new URL(defaultRouteForSession(session, preferredTenantId), request.url));
  }

  if (
    session &&
    principal?.principal_type !== "platform_super_admin" &&
    (tenantScopedSurface === "workflows" || tenantScopedSurface === "executions")
  ) {
    return NextResponse.redirect(new URL(defaultRouteForSession(session, preferredTenantId), request.url));
  }

  return NextResponse.next();
});

export const config = {
  matcher: [
    /*
     * Match all paths except API, Next internals, and common static assets.
     * See https://nextjs.org/docs/app/api-reference/file-conventions/proxy#matcher
     */
    "/((?!api|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
