const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function compile(relativePath) {
  return ts.transpileModule(fs.readFileSync(path.join(__dirname, "../..", relativePath), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
}

function loadSessionPolicy() {
  const exports = {};
  vm.runInNewContext(compile("lib/auth-session.ts"), { exports });
  return exports;
}

function loadAuth() {
  let configuration;
  vm.runInNewContext(compile("auth.ts"), {
    exports: {},
    require(name) {
      if (name === "next-auth") return { default: (value) => { configuration = value; return {}; } };
      if (name === "next-auth/providers/credentials") return { default: (value) => value };
      if (name === "@/lib/auth-secret") return { requireAuthSecret: () => "UNIT_TEST_SECRET_UNUSED_BY_STUB" };
      if (name === "@/lib/server-api") return { SERVER_API_BASE_URL: "http://localhost:60001" };
      if (name === "@/lib/auth-session") return loadSessionPolicy();
      throw new Error(`Unexpected dependency ${name}`);
    }
  });
  return configuration.callbacks;
}

const admin = { principal_type: "platform_super_admin", username: "admin", memberships: [] };
const membership = {
  membership_id: "membership-one", tenant_id: "tenant-one", role: "technical_member",
  permission_keys: ["technical.access"], effective_mode: "technical", mode_override: null,
  onboarding_kind: "member_join", first_signed_in_at: null, onboarding_completed_at: null,
  onboarding_version: null, team_ids: [], discord_state: {},
};
const tenant = { principal_type: "tenant_user", user_id: "user-one", email: "user@example.invalid", memberships: [membership] };
const bearer = "INVALID_TEST_ACCESS_TOKEN";

for (const [name, token] of [
  ["absent principal", { accessToken: bearer }],
  ["null principal", { accessToken: bearer, principal: null }],
  ["string principal", { accessToken: bearer, principal: "platform_super_admin" }],
  ["array principal", { accessToken: bearer, principal: [] }],
  ["unknown role", { accessToken: bearer, principal: { ...admin, principal_type: "admin" } }],
  ["missing admin identity", { accessToken: bearer, principal: { ...admin, username: null } }],
  ["blank admin identity", { accessToken: bearer, principal: { ...admin, username: "   " } }],
  ["admin membership invention", { accessToken: bearer, principal: { ...admin, memberships: [membership] } }],
  ["missing membership array", { accessToken: bearer, principal: { ...admin, memberships: null } }],
  ["missing tenant identity", { accessToken: bearer, principal: { ...tenant, user_id: null } }],
  ["missing tenant email", { accessToken: bearer, principal: { ...tenant, email: null } }],
  ["malformed profile field", { accessToken: bearer, principal: { ...tenant, full_name: {} } }],
  ["empty tenant memberships", { accessToken: bearer, principal: { ...tenant, memberships: [] } }],
  ["invalid permission keys", { accessToken: bearer, principal: { ...tenant, memberships: [{ ...membership, permission_keys: "technical.access" }] } }],
  ["invalid effective mode", { accessToken: bearer, principal: { ...tenant, memberships: [{ ...membership, effective_mode: "unknown" }] } }],
  ...Object.entries({
    membership_id: "", tenant_id: "", role: "", permission_keys: [null], mode_override: "unknown",
    onboarding_kind: "unknown", first_signed_in_at: {}, onboarding_completed_at: undefined,
    onboarding_version: 7, team_ids: [null], discord_state: [],
  }).map(([field, value]) => [`invalid membership ${field}`, {
    accessToken: bearer, principal: { ...tenant, memberships: [{ ...membership, [field]: value }] },
  }]),
  ["absent bearer", { principal: admin }],
  ["null bearer", { principal: admin, accessToken: null }],
  ["empty bearer", { principal: admin, accessToken: "" }],
  ["blank bearer", { principal: admin, accessToken: "   " }],
  ["object bearer", { principal: admin, accessToken: {} }],
]) {
  test(`${name} rejects decrypted JWT before a session is granted`, async () => {
    await assert.rejects(loadAuth().jwt({ token }), /Invalid authentication session/);
  });
  test(`${name} cannot become a principal in a session callback`, async () => {
    const session = { user: { name: "Original" } };
    await assert.rejects(loadAuth().session({ session, token }), /Invalid authentication session/);
    assert.deepEqual(session, { user: { name: "Original" } });
  });
}

for (const principal of [admin, tenant]) {
  test(`valid ${principal.principal_type} survives JWT refresh and public session translation`, async () => {
    const callbacks = loadAuth();
    const token = { principal, accessToken: bearer };
    assert.equal(await callbacks.jwt({ token }), token);
    const session = await callbacks.session({ session: { user: { name: "Original" } }, token });
    assert.equal(session.user.principal, principal);
    assert.equal(session.user.accessToken, undefined);
  });
  test(`valid ${principal.principal_type} is copied from the authenticated user on initial JWT issuance`, async () => {
    const token = {};
    await loadAuth().jwt({ token, user: { principal, accessToken: bearer } });
    assert.equal(token.principal, principal);
    assert.equal(token.accessToken, bearer);
  });
}

test("malformed authenticated user cannot issue an accepted JWT", async () => {
  await assert.rejects(loadAuth().jwt({ token: {}, user: { accessToken: bearer } }), /Invalid authentication session/);
});

test("malformed decrypted BFF identity fails before an authenticated backend request", async () => {
  const exports = {};
  let backendRequests = 0;
  vm.runInNewContext(compile("app/api/bff/[...path]/route.ts"), {
    exports,
    URL,
    Headers,
    fetch: () => { backendRequests++; throw new Error("Backend must not be contacted"); },
    require(name) {
      if (name === "next/server") return { NextResponse: { json: (body, options) => ({ body, status: options.status }) } };
      // Token decryption is the framework boundary; the owned BFF handler and policy are real.
      if (name === "next-auth/jwt") return { getToken: async () => ({ accessToken: bearer }) };
      if (name === "@/lib/auth-secret") return { requireAuthSecret: () => "UNIT_TEST_SECRET_UNUSED_BY_STUB" };
      if (name === "@/lib/auth-session") return loadSessionPolicy();
      if (name === "@/lib/server-api") return { SERVER_API_BASE_URL: "http://localhost:60001" };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  const request = { method: "GET", headers: new Headers(), nextUrl: new URL("http://app.example/api/bff/api/admin/auth/me") };
  const response = await exports.GET(request, { params: Promise.resolve({ path: ["api", "admin", "auth", "me"] }) });
  assert.equal(response.status, 401);
  assert.equal(response.body.detail, "Authentication required");
  assert.equal(backendRequests, 0);
});
