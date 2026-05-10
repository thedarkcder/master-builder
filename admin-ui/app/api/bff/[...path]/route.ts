import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

import { DEFAULT_API_BASE_URL } from "@/lib/auth-constants";

function buildBackendUrl(request: NextRequest, path: string[]): string {
  const base = DEFAULT_API_BASE_URL.replace(/\/$/, "");
  const joinedPath = path.map(encodeURIComponent).join("/");
  const url = new URL(`${base}/${joinedPath}`);
  request.nextUrl.searchParams.forEach((value, key) => {
    url.searchParams.append(key, value);
  });
  return url.toString();
}

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const token = await getToken({
    req: request,
    secret: process.env.AUTH_SECRET ?? process.env.NEXTAUTH_SECRET ?? "local-dev-authjs-secret",
  });
  const accessToken = typeof token?.accessToken === "string" ? token.accessToken : "";
  if (!accessToken) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const { path } = await context.params;
  const targetUrl = buildBackendUrl(request, path);
  const headers = new Headers();
  headers.set("Authorization", `Bearer ${accessToken}`);
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("Content-Type", contentType);
  }
  const accept = request.headers.get("accept");
  if (accept) {
    headers.set("Accept", accept);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    signal: request.signal,
    cache: "no-store",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.text();
  }

  const response = await fetch(targetUrl, init);
  const proxiedHeaders = new Headers();
  const responseContentType = response.headers.get("content-type");
  if (responseContentType) {
    proxiedHeaders.set("Content-Type", responseContentType);
  }
  return new NextResponse(response.body, {
    status: response.status,
    headers: proxiedHeaders,
  });
}

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, context);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, context);
}

export async function PUT(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, context);
}

export async function DELETE(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, context);
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return proxy(request, context);
}
