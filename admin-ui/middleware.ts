import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import type { Session } from "next-auth";

import { auth } from "@/auth";

export default auth((request: NextRequest & { auth: Session | null }) => {
  const { pathname } = request.nextUrl;
  const isPublicPath =
    pathname === "/login" || pathname === "/register" || pathname === "/privacy" || pathname.startsWith("/invite/accept");
  const session = request.auth;

  if (pathname.startsWith("/_next") || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  if (!session && !isPublicPath) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  if (session && (pathname === "/login" || pathname === "/register")) {
    return NextResponse.redirect(new URL("/dashboard", request.url));
  }

  return NextResponse.next();
});

export const config = {
  matcher: ["/((?!api).*)"]
};
