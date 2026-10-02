import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

import { SERVER_API_BASE_URL } from "@/lib/server-api";
import { requireAuthSecret } from "@/lib/auth-secret";

function buildBackendUrl(request: NextRequest, path: string[]): string {
  const base = SERVER_API_BASE_URL.replace(/\/$/, "");
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
    secret: requireAuthSecret(),
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

  const range = request.headers.get("range");
  if (range) headers.set("Range", range);

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
  for (const name of ["content-range", "accept-ranges", "content-length", "cache-control", "content-disposition", "x-content-type-options", "content-security-policy", "retry-after"]) {
    const value = response.headers.get(name);
    if (value) proxiedHeaders.set(name, value);
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
