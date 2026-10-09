import assert from 'node:assert/strict';
import test from 'node:test';
import {inspectDependencies,inspectModelDependencies} from '../src/dependencies.js';

const available=new Set(['src/alpha.imba','src/beta.imba','src/api.pb.imba']);
test('startup and every lazy callback have separate complete scopes',()=>{
  const before=inspectDependencies(`require('./alpha.js'); routerAdd('GET','/api/beta',e=>e.json(200,require('./beta.js')));`,'src/api.pb.imba',available);
  const after=inspectDependencies(`require('./alpha.js'); routerAdd('GET','/api/beta',e=>e.json(201,require('./beta.js')));`,'src/api.pb.imba',available);
  assert.deepEqual(before.global.roots,['src/alpha.imba']);
  assert.deepEqual(before.routes[0].roots,['src/beta.imba']);
  assert.equal(before.startup_hash,after.startup_hash);
  assert.notEqual(before.request_hashes[0],after.request_hashes[0]);
});
test('conditional imports cover both unvisited branches',()=>{
  const scope=inspectDependencies(`require(flag ? './alpha.js' : './beta.js')`,'src/api.pb.imba',available);
  assert.deepEqual(scope.global.roots,['src/alpha.imba','src/beta.imba']);
  assert.equal(scope.global.opaque,false);
});
test('dynamic and aliased loaders are opaque',()=>{
  for(const code of ['require(path)','const loader=require; loader("./alpha.js")','eval(code)'])
    assert.equal(inspectDependencies(code,'src/api.pb.imba',available).global.opaque,true);
});
test('model scopes include literal lists, imported helpers and file reads',()=>{
  const scope=inspectModelDependencies(`import {load} from './helper.js'; import {readFileSync} from 'node:fs'; for(const name of ['alpha','beta']) load(name+'.js'); readFileSync('fixture.json');`,'test/one.js',available);
  assert.equal(scope.opaque,false);
  assert.deepEqual(scope.roots,['src/alpha.imba','src/beta.imba']);
  assert.deepEqual(scope.imports,['test/helper.js']);
  assert.deepEqual(scope.files,['fixture.json']);
});
test('unknown data reads retain a broad scope',()=>{
  const scope=inspectModelDependencies(`import {readFileSync} from 'node:fs'; readFileSync(process.env.FILE);`,'test/one.js',available);
  assert.equal(scope.opaque,true);
});
test('source and output directories belong to the consumer contract',()=>{
  const scope=inspectDependencies(`require('{__hooks}/alpha.js')`,'hooks/api.pb.imba',new Set(['hooks/alpha.imba']),{sources:'hooks',outdir:'build'});
  assert.deepEqual(scope.global.roots,['hooks/alpha.imba']);
  assert.equal(scope.global.opaque,false);
});
