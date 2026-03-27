import { NextRequest, NextResponse } from "next/server";

import { DEFAULT_API_BASE_URL } from "@/lib/auth-constants";

export const dynamic = "force-dynamic";

export async function POST(request: NextRequest) {
  const base = DEFAULT_API_BASE_URL.replace(/\/$/, "");
  const targetUrl = `${base}/api/public/register`;
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
