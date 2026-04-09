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
  const isPublicPath =
    pathname === "/" ||
    pathname === "/login" ||
    pathname === "/register" ||
    pathname === "/forgot-password" ||
    pathname === "/reset-password" ||
    pathname === "/privacy" ||
    pathname.startsWith("/invite/accept");
  const session = request.auth;
  const preferredTenantId = request.cookies.get(getLastWorkspaceCookieName())?.value ?? null;

  if (pathname.startsWith("/_next") || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  if (!session && !isPublicPath) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  if (session && (pathname === "/login" || pathname === "/register")) {
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
