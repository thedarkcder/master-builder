import { NextRequest, NextResponse } from "next/server";

import { SERVER_API_BASE_URL } from "@/lib/server-api";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const base = SERVER_API_BASE_URL.replace(/\/$/, "");
  const targetUrl = `${base}/api/public/invites/accept`;
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("Content-Type", contentType);
  }
  const accept = request.headers.get("accept");
  if (accept) {
    headers.set("Accept", accept);
  }

  const response = await fetch(targetUrl, {
    method: "POST",
    headers,
    body: await request.text(),
    redirect: "manual",
  });

  const body = await response.text();
  const proxiedHeaders = new Headers();
  const responseContentType = response.headers.get("content-type");
  if (responseContentType) {
    proxiedHeaders.set("Content-Type", responseContentType);
  }
  return new NextResponse(body, {
    status: response.status,
    headers: proxiedHeaders,
  });
}
