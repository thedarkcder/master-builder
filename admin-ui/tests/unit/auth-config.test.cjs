const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

// Exercise the real auth configuration. Only the external Auth.js provider is
// replaced, so missing server configuration is checked without a live backend.
function loadSecretPolicy(environment) {
  const source = fs.readFileSync(path.join(__dirname, "../../lib/auth-secret.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, process: { env: environment } });
  return exports;
}

function loadAuthConfig(environment) {
  const source = fs.readFileSync(path.join(__dirname, "../../auth.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
  let configuration;
  vm.runInNewContext(compiled, {
    exports: {},
    process: { env: environment },
    require(name) {
      if (name === "next-auth") return { default: (value) => { configuration = value; return {}; } };
      if (name === "next-auth/providers/credentials") return { default: (value) => value };
      if (name === "@/lib/auth-secret") return loadSecretPolicy(environment);
      if (name === "@/lib/server-api") return { SERVER_API_BASE_URL: "http://localhost:60001" };
      throw new Error(`Unexpected dependency ${name}`);
    }
  });
  return configuration;
}

for (const environment of [
  {},
  { AUTH_SECRET: "" },
  { AUTH_SECRET: "change-me" },
  { AUTH_SECRET: " ".repeat(32) },
  { NEXTAUTH_SECRET: crypto.randomBytes(32).toString("hex") }
]) {
  test("Auth.js refuses missing, weak, or legacy-only AUTH_SECRET configuration", () => {
    assert.throws(() => loadAuthConfig(environment), /AUTH_SECRET/);
  });
}

test("Auth.js uses the explicitly configured AUTH_SECRET", () => {
  const secret = crypto.randomBytes(32).toString("hex");
  assert.equal(loadAuthConfig({ AUTH_SECRET: secret }).secret, secret);
});

function loadBff(environment) {
  const source = fs.readFileSync(path.join(__dirname, "../../app/api/bff/[...path]/route.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    process: { env: environment },
    require(name) {
      if (name === "next/server") return { NextResponse: { json: (body, options) => ({ body, status: options.status }) } };
      if (name === "next-auth/jwt") return { getToken: async () => null };
      if (name === "@/lib/auth-secret") return loadSecretPolicy(environment);
      if (name === "@/lib/server-api") return { SERVER_API_BASE_URL: "http://localhost:60001" };
      throw new Error(`Unexpected dependency ${name}`);
    }
  });
  return exports;
}

for (const environment of [{}, { AUTH_SECRET: "" }, { AUTH_SECRET: "change-me" }, { NEXTAUTH_SECRET: crypto.randomBytes(32).toString("hex") }]) {
  test("BFF refuses missing, weak, or legacy-only AUTH_SECRET configuration", async () => {
    await assert.rejects(loadBff(environment).GET({}, { params: Promise.resolve({ path: [] }) }), /AUTH_SECRET/);
  });
}
