import {readFileSync} from 'node:fs';

export class TestContext {
  static current() {
    if (!process.env.PBGATE_CONTEXT) throw Error('This test needs a PocketBase fixture; register it in pbgate.json');
    return new TestContext(JSON.parse(readFileSync(process.env.PBGATE_CONTEXT, 'utf8')));
  }

  constructor(descriptor) {
    const url = new URL(descriptor.url);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1') throw Error('Fixture must use owned loopback HTTP');
    this.url = descriptor.url;
    this.check = descriptor.check;
    this.adminToken = descriptor.adminToken;
    this.adminEmail = descriptor.adminEmail;
    this.adminPassword = descriptor.adminPassword;
    this.dataDirectory = descriptor.dataDirectory;
    Object.freeze(this);
  }

  async request(path, {method, body, auth = this.adminToken, timeout = 10000, headers = {}} = {}) {
    if (!path.startsWith('/') || path.startsWith('//')) throw Error('Use a fixture-relative API path');
    const response = await fetch(this.url + path, {
      method: method || (body === undefined ? 'GET' : 'POST'),
      headers: {...headers, 'Content-Type': 'application/json', ...(auth ? {Authorization:auth} : {})},
      body: body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(timeout),
      redirect:'error',
    });
    const payload = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const error = Error(`Fixture request ${response.status}: ${payload.message || response.statusText}`);
      Object.assign(error, {status:response.status, data:payload});
      throw error;
    }
    return payload;
  }
}
