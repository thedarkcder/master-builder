const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function loadPolicy() {
  const source = fs.readFileSync(path.join(__dirname, "../../lib/local-navigation-destination.ts"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, URL });
  return exports;
}

for (const absent of [null, undefined]) {
  test("an absent navigation destination preserves the caller's default policy", () => {
    assert.equal(loadPolicy().getLocalNavigationDestination(absent), null);
  });
}

for (const [name, input] of [
  ["empty", ""], ["absolute", "https://external.example/"], ["same-origin absolute", "https://navigation.invalid/privacy"],
  ["relative", "privacy"], ["protocol-relative", "//external.example"],
  ["backslash authority", "/\\external.example"], ["backslash path", "/local\\external.example"],
  ["tab authority", "/\texternal.example"], ["newline authority", "/\n/external.example"],
  ["carriage return", "/\r/external.example"], ["NUL", "/\0external.example"], ["DEL", "/\u007fexternal.example"],
  ["encoded backslash", "/%5cexternal.example"], ["encoded slash authority", "/%2fexternal.example"],
  ["encoded control", "/%0a/external.example"], ["normalized authority", "/local/..//external.example"],
  ["dot authority", "/.//external.example"], ["malformed escape", "/%zz"], ["script scheme", "javascript:alert(1)"],
]) {
  test(`${name} destination fails explicitly without substituting a route`, () => {
    const { getLocalNavigationDestination } = loadPolicy();
    assert.throws(() => getLocalNavigationDestination(input), /Invalid navigation destination/);
  });
}

for (const [input, expected] of [
  ["/", "/"], ["/privacy", "/privacy"], ["/privacy?from=login#policy", "/privacy?from=login#policy"],
  ["/local/../privacy", "/privacy"], ["/search?q=hello%20world", "/search?q=hello%20world"],
  ["/search?url=https%3A%2F%2Fexternal.example", "/search?url=https%3A%2F%2Fexternal.example"],
]) {
  test(`valid local destination ${input} remains local after canonicalization`, () => {
    const actual = loadPolicy().getLocalNavigationDestination(input);
    assert.equal(actual, expected);
    for (const origin of ["http://localhost:51647", "https://app.example"]) {
      assert.equal(new URL(actual, origin).origin, origin);
    }
  });
}

// Exercise the real provider entry point. React rendering and the external
// Auth.js credential sender are boundaries; the owned login callback is real.
test("provider rejects an unsafe target before auth, state or navigation side effects", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../../components/auth-provider.tsx"), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  const exports = {};
  const effects = [];
  const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
  vm.runInNewContext(compiled, {
    exports,
    window: { location: { assign: () => effects.push("navigation") } },
    require(name) {
      if (name === "react/jsx-runtime") return { jsx };
      if (name === "react") return {
        createContext: () => ({ Provider: "ContextProvider" }),
        useState: (value) => [value, () => effects.push("state")],
        useMemo: (read) => read(),
        useEffect: () => {},
      };
      if (name === "next-auth/react") return {
        SessionProvider: ({ children }) => children,
        useSession: () => ({ status: "unauthenticated" }),
        signIn: async () => { effects.push("credentials"); return { ok: true }; },
      };
      if (name === "@/lib/local-navigation-destination") return loadPolicy();
      if (name === "@/lib/auth-redirect-barrier") return { clearLogoutRedirectBarrier: () => effects.push("barrier") };
      if (name === "@/lib/api") return {};
      throw new Error(`Unexpected dependency ${name}`);
    }
  });
  const context = exports.AuthProvider({ children: null });
  await assert.rejects(context.props.value.login({
    identifier: "redirect-test", password: "INVALID_TEST_PASSWORD", redirectTo: "/\\external.example"
  }), /Invalid navigation destination/);
  assert.deepEqual(effects, []);
});
