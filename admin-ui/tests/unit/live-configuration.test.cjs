const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
function load(environment) {
 const source=fs.readFileSync(path.join(__dirname, '../e2e/support/live-backend.ts'),'utf8');
 const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
 const exports={};
 vm.runInNewContext(compiled,{exports,process:{env:environment},Buffer,require(name){if(name==='node:child_process')return {}; throw new Error(name);}});
 return exports;
}
test('live cleanup requires configured credentials instead of a default password',async()=>{
 const request={post:async()=>{throw new Error('Network should not be reached without credentials');}};
 await assert.rejects(load({}).archiveTenant(request,'synthetic-tenant'),/ORCHESTRATOR_ADMIN_PASSWORD/);
});
