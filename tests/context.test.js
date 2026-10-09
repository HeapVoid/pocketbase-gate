import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {TestContext} from '../src/context.js';

test('test context confines requests to its fixture and reports real API failures', async () => {
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/empty') {response.writeHead(204); response.end();}
    else if (request.url === '/failure') {response.writeHead(403); response.end(JSON.stringify({message:'Denied'}));}
    else response.end(JSON.stringify({auth:request.headers.authorization || null}));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const context = new TestContext({url:`http://127.0.0.1:${server.address().port}`, adminToken:'test-token'});
    assert.deepEqual(await context.request('/ok'), {auth:'test-token'});
    assert.deepEqual(await context.request('/ok', {auth:null}), {auth:null});
    assert.equal(await context.request('/empty', {method:'DELETE'}), null);
    await assert.rejects(context.request('/failure'), error => error.status === 403);
    await assert.rejects(context.request('https://example.com'), /relative/);
    await assert.rejects(context.request('//example.com'), /relative/);
    assert.throws(() => new TestContext({url:'https://example.com'}), /loopback/);
    assert.throws(() => TestContext.current(), /register/);
  } finally {server.closeAllConnections(); await new Promise(resolve => server.close(resolve));}
});
