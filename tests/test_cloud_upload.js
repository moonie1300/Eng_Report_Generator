const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const script = readFileSync(join(__dirname, '../static/cloud-upload.js'), 'utf8');
const config = {
  cognitoDomain: 'https://auth.example.com',
  clientId: 'client',
  redirectUri: 'https://example.com',
  uploadApiUrl: 'https://api.example.com/upload-url',
};

function setup({ initial = {}, search = '', fetchImpl, origin = 'https://example.com' } = {}) {
  const elements = {};
  for (const id of ['sign-in', 'sign-out', 'cloud-upload', 'cloud-file', 'upload-button', 'cloud-status', 'cloud-error']) {
    elements[id] = {
      hidden: false, disabled: false, textContent: '', files: [],
      addEventListener(type, handler) { this[type] = handler; },
    };
  }
  const values = new Map(Object.entries(initial));
  const sessionStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
  const location = {
    origin, pathname: '/', search, hash: '',
    assign(url) { this.assigned = url; },
  };
  const history = { replaceState() { location.search = ''; } };
  const context = {
    AWS_CONFIG: config, document: { getElementById: (id) => elements[id] },
    sessionStorage, location, history, crypto: webcrypto, TextEncoder,
    Uint8Array, URL, URLSearchParams, Date, Number, String, Boolean,
    btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
    fetch: fetchImpl || (() => { throw new Error('Unexpected fetch'); }),
  };
  vm.runInNewContext(script, context);
  return { elements, values, location };
}

const signedIn = {
  'battery.accessToken': 'test-access-token',
  'battery.expiresAt': String(Date.now() + 60000),
};

test('sign-in uses authorization code and PKCE', async () => {
  const { elements, values, location } = setup();
  await elements['sign-in'].click();
  const url = new URL(location.assigned);
  assert.equal(url.pathname, '/oauth2/authorize');
  assert.equal(url.searchParams.get('response_type'), 'code');
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(url.searchParams.get('state'), values.get('battery.oauthState'));
  assert.ok(values.get('battery.pkceVerifier'));
  assert.ok(url.searchParams.get('code_challenge'));
});

test('callback validates state and exchanges code for an access token', async () => {
  const calls = [];
  const { values, location, elements } = setup({
    initial: { 'battery.oauthState': 'state', 'battery.pkceVerifier': 'verifier' },
    search: '?code=code&state=state',
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({ access_token: 'access', expires_in: 300 }) };
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls[0].url, 'https://auth.example.com/oauth2/token');
  assert.equal(calls[0].options.body.get('code_verifier'), 'verifier');
  assert.equal(values.get('battery.accessToken'), 'access');
  assert.equal(values.has('battery.oauthState'), false);
  assert.equal(location.search, '');
  assert.equal(elements['cloud-upload'].hidden, false);
});

test('callback rejects a mismatched state without exchanging the code', async () => {
  const { elements, values } = setup({
    initial: { 'battery.oauthState': 'expected', 'battery.pkceVerifier': 'verifier' },
    search: '?code=code&state=wrong',
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(elements['cloud-error'].textContent, /could not be verified/);
  assert.equal(values.has('battery.accessToken'), false);
});

test('uploads raw file using a presigned PUT and displays the job ID', async () => {
  const calls = [];
  const file = { name: 'data.csv', size: 12 };
  const { elements } = setup({
    initial: signedIn,
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      if (calls.length === 1) {
        return { ok: true, json: async () => ({ job_id: 'job-123', upload_url: 'https://s3.example.com/signed' }) };
      }
      return { ok: true, status: 200 };
    },
  });
  elements['cloud-file'].files = [file];
  await elements['cloud-upload'].submit({ preventDefault() {} });
  assert.equal(calls[0].url, config.uploadApiUrl);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer test-access-token');
  assert.equal(calls[1].options.method, 'PUT');
  assert.equal(calls[1].options.headers['Content-Type'], 'text/csv');
  assert.equal(calls[1].options.body, file);
  assert.equal(calls[1].options.headers.Authorization, undefined);
  assert.match(elements['cloud-status'].textContent, /Upload successful.*job-123/);
});

test('reports S3 failure without showing upload success', async () => {
  let calls = 0;
  const { elements } = setup({
    initial: signedIn,
    fetchImpl: async () => ++calls === 1
      ? { ok: true, json: async () => ({ job_id: 'job', upload_url: 'https://s3.example.com/signed' }) }
      : { ok: false, status: 403 },
  });
  elements['cloud-file'].files = [{ size: 12 }];
  await elements['cloud-upload'].submit({ preventDefault() {} });
  assert.match(elements['cloud-error'].textContent, /S3 upload failed/);
  assert.equal(elements['cloud-status'].textContent, '');
});

test('authorization failure stops before uploading', async () => {
  let calls = 0;
  const { elements } = setup({
    initial: signedIn,
    fetchImpl: async () => { calls++; return { ok: false, status: 401 }; },
  });
  elements['cloud-file'].files = [{ size: 12 }];
  await elements['cloud-upload'].submit({ preventDefault() {} });
  assert.equal(calls, 1);
  assert.match(elements['cloud-error'].textContent, /authorization failed/);
  assert.equal(elements['cloud-status'].textContent, '');
});

test('network errors point to connectivity and CORS', async () => {
  const { elements } = setup({
    initial: signedIn,
    fetchImpl: async () => { throw new TypeError('Failed to fetch'); },
  });
  elements['cloud-file'].files = [{ size: 12 }];
  await elements['cloud-upload'].submit({ preventDefault() {} });
  assert.match(elements['cloud-error'].textContent, /CORS/);
});

test('expired sign-in blocks uploads', async () => {
  const { elements } = setup({ initial: { ...signedIn, 'battery.expiresAt': '1' } });
  elements['cloud-file'].files = [{ size: 12 }];
  await elements['cloud-upload'].submit({ preventDefault() {} });
  assert.match(elements['cloud-error'].textContent, /expired/);
  assert.equal(elements['cloud-upload'].hidden, true);
});

test('local origin does not redirect to an incompatible Cognito callback', async () => {
  const { elements, location } = setup({ origin: 'http://127.0.0.1:8000' });
  await elements['sign-in'].click();
  assert.match(elements['cloud-error'].textContent, /CloudFront callback URL/);
  assert.equal(location.assigned, undefined);
});
