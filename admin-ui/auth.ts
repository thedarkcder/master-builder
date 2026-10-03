import NextAuth, { type Session, type User } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import type { JWT } from "next-auth/jwt";

import { SERVER_API_BASE_URL } from "@/lib/server-api";
import { requireAuthSecret } from "@/lib/auth-secret";
import { requireAuthSession } from "@/lib/auth-session";
import type { AuthenticatedPrincipalRecord as PrincipalPayload } from "@/lib/api";

type AuthorizedUser = {
  id: string;
  name: string;
  email: string | null;
  accessToken: string;
  principal: PrincipalPayload;
};

async function loginAgainstBackend(identifier: string, password: string): Promise<AuthorizedUser | null> {
  const base = SERVER_API_BASE_URL.replace(/\/$/, "");
  if (identifier.includes("@")) {
    const response = await fetch(`${base}/api/app/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: identifier, password })
    });
    if (!response.ok) {
      return null;
    }
    const payload = (await response.json()) as {
      access_token: string;
      principal: PrincipalPayload;
    };
    return {
      id: payload.principal.user_id ?? payload.principal.email ?? identifier,
      name: payload.principal.full_name ?? payload.principal.email ?? identifier,
      email: payload.principal.email ?? identifier,
      accessToken: payload.access_token,
      principal: payload.principal
    };
  }

  const loginResponse = await fetch(`${base}/api/admin/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: identifier, password })
  });
  if (!loginResponse.ok) {
    return null;
  }
  const loginPayload = (await loginResponse.json()) as { access_token: string };
  const meResponse = await fetch(`${base}/api/admin/auth/me`, {
    headers: {
      Authorization: `Bearer ${loginPayload.access_token}`
    }
  });
  if (!meResponse.ok) {
    return null;
  }
  const mePayload = (await meResponse.json()) as { username: string };
    return {
      id: mePayload.username,
      name: mePayload.username,
      email: null,
      accessToken: loginPayload.access_token,
      principal: {
        principal_type: "platform_super_admin" as const,
        username: mePayload.username,
        memberships: []
      }
  };
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  secret: requireAuthSecret(),
  pages: {
    signIn: "/login",
  },
  session: {
    strategy: "jwt"
  },
  providers: [
    Credentials({
      name: "Master Builder",
      credentials: {
        identifier: { label: "Email or username", type: "text" },
        password: { label: "Password", type: "password" }
      },
      async authorize(credentials) {
        const identifier = String(credentials?.identifier ?? "").trim();
        const password = String(credentials?.password ?? "");
        if (!identifier || !password) {
          return null;
        }
        return loginAgainstBackend(identifier, password);
      }
    })
  ],
  callbacks: {
    authorized({ auth, request }) {
      const pathname = request.nextUrl.pathname;
      const isPublicPath =
        pathname === "/" ||
        pathname === "/login" ||
        pathname === "/forgot-password" ||
        pathname === "/reset-password" ||
        pathname === "/register" ||
        pathname === "/privacy" ||
        pathname.startsWith("/invite/accept");
      return isPublicPath || Boolean(auth);
    },
    async jwt({ token, user }: { token: JWT; user?: User }) {
      if (user) {
        token.accessToken = (user as { accessToken?: string }).accessToken;
        token.principal = (user as { principal?: PrincipalPayload }).principal;
      }
      requireAuthSession(token);
      return token;
    },
    async session({ session, token }: { session: Session; token: JWT }) {
      session.user = {
        ...session.user,
        principal: requireAuthSession(token).principal
      };
      return session;
    }
  }
});
