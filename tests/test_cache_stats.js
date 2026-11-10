const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

const script = readFileSync(join(__dirname, '../static/cache-stats.js'), 'utf8');

function setup(fetch) {
  const elements = {};
  for (const id of ['cache-stats', 'cache-error', 'cache-refresh', 'cache-reset']) {
    elements[id] = {
      textContent: '', disabled: false,
      addEventListener(type, handler) { this[type] = handler; },
    };
  }
  vm.runInNewContext(script, { document: { getElementById: (id) => elements[id] }, fetch });
  return elements;
}

test('displays stats and resets through protected POST', async () => {
  const calls = [];
  const elements = setup(async (url, options) => {
    calls.push({ url, options });
    return { ok: true, json: async () => ({
      ttl_seconds: 60, cached_entries: 1, hits: 3, misses: 1, dynamodb_reads: 1,
      failed_requests: 0, average_hit_ms: 0.1, average_miss_ms: 15,
      last_response: { result: 'hit', duration_ms: 0.1 },
    }) };
  });
  await elements['cache-refresh'].click();
  assert.match(elements['cache-stats'].textContent, /Hits: 3 \| Misses: 1 \| DynamoDB reads: 1/);
  assert.match(elements['cache-stats'].textContent, /Average miss: 15.00 ms/);
  await elements['cache-reset'].click();
  assert.equal(calls[1].url, '/cloud/cache/reset');
  assert.equal(calls[1].options.method, 'POST');
  assert.equal(calls[1].options.headers['X-Local-Report'], '1');
});

test('displays failed statistics requests', async () => {
  const elements = setup(async () => ({ ok: false, json: async () => ({ error: 'Forbidden' }) }));
  await elements['cache-refresh'].click();
  assert.match(elements['cache-error'].textContent, /Forbidden/);
  assert.equal(elements['cache-refresh'].disabled, false);
});
