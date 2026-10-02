const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
function load(environment) {
  const source = fs.readFileSync(path.join(__dirname, '../../lib/server-api.ts'), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, URL, process: { env: environment } });
  return exports.SERVER_API_BASE_URL;
}
for (const environment of [{}, { NEXT_PUBLIC_API_BASE_URL: 'http://localhost:60001' }, { ORCHESTRATOR_API_BASE_URL: '' }, { ORCHESTRATOR_API_BASE_URL: 'file:///private' }, { ORCHESTRATOR_API_BASE_URL: 'http://user:password@api:4000' }]) {
  test('server addressing requires an explicit valid service URL', () => assert.throws(() => load(environment), /ORCHESTRATOR_API_BASE_URL/));
}
test('container server URL is independent of public build-time browser URL', () => {
  assert.equal(load({ ORCHESTRATOR_API_BASE_URL: 'http://api:4000/', NEXT_PUBLIC_API_BASE_URL: 'https://public.example.com' }), 'http://api:4000');
});
