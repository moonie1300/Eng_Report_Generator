const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');

const script = readFileSync(join(__dirname, '../static/cloud-jobs.js'), 'utf8');
const jobId = '12345678-1234-1234-1234-123456789abc';

function setup(fetchImpl, confirm = () => true) {
  const elements = {};
  for (const id of ['job-form', 'job-file', 'max-voltage', 'max-current', 'submit-job', 'lookup-form', 'lookup-id', 'job', 'job-id', 'job-status',
    'error', 'start-worker', 'worker-warning', 'refresh', 'task', 'report', 'step-queued', 'step-processing', 'step-completed']) {
    const classes = new Set();
    const attributes = {};
    elements[id] = {
      hidden: false, disabled: false, textContent: '', value: '', files: [], attributes,
      classList: {
        toggle(name, enabled) { if (enabled) classes.add(name); else classes.delete(name); },
        contains(name) { return classes.has(name); },
      },
      setAttribute(name, value) { attributes[name] = value; },
      removeAttribute(name) { delete attributes[name]; },
      addEventListener(type, handler) { this[type] = handler; },
    };
  }
  const values = new Map();
  const timers = [];
  const context = {
    document: { getElementById: (id) => elements[id] },
    sessionStorage: {
      getItem: (key) => values.get(key) || null,
      setItem: (key, value) => values.set(key, value),
    },
    crypto: { randomUUID: () => jobId }, TextDecoder, fetch: fetchImpl, confirm,
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout() {}, encodeURIComponent,
  };
  vm.runInNewContext(script, context);
  return { elements, values, timers };
}

test('uploads, queues, starts one confirmed task and exposes completed PDF', async () => {
  const calls = [];
  let state = 'QUEUED';
  const { elements, values } = setup(async (url, options) => {
    calls.push({ url, options });
    if (url === '/cloud/start-worker') return { ok: true, json: async () => ({ task_arn: 'task-1' }) };
    if (url === '/cloud/jobs') return { ok: true, json: async () => ({ job_id: jobId, status: state }) };
    return { ok: true, json: async () => ({ job_id: jobId, status: state }) };
  });
  elements['job-file'].files = [{ arrayBuffer: async () => Buffer.from('timestamp,voltage_v\n') }];
  elements['max-voltage'].value = '60';
  elements['max-current'].value = '40';
  await elements['job-form'].submit({ preventDefault() {} });
  assert.equal(values.get('report.cloudJobId'), jobId);
  assert.equal(elements['job-status'].textContent, 'QUEUED');
  assert.equal(elements['start-worker'].hidden, false);
  assert.equal(JSON.parse(calls[0].options.body).job_id, jobId);
  assert.equal(JSON.parse(calls[0].options.body).max_voltage, '60');
  assert.equal(JSON.parse(calls[0].options.body).max_abs_current, '40');
  assert.equal(elements['step-queued'].attributes['aria-current'], 'step');
  assert.equal(elements['worker-warning'].hidden, false);
  assert.equal(calls[0].options.headers['X-Local-Report'], '1');
  await elements['start-worker'].click();
  assert.match(elements.task.textContent, /task-1/);
  assert.equal(calls.filter((call) => call.url === '/cloud/start-worker').length, 1);
  state = 'PROCESSING';
  await elements.refresh.click();
  assert.equal(elements['step-processing'].attributes['aria-current'], 'step');
  assert.equal(elements['step-queued'].attributes['aria-current'], undefined);
  assert.equal(elements['worker-warning'].hidden, true);
  state = 'COMPLETED';
  await elements.refresh.click();
  assert.equal(elements['job-status'].textContent, 'COMPLETED');
  assert.equal(elements.report.hidden, false);
  assert.equal(elements.report.href, `/cloud/jobs/${jobId}/report`);
  assert.equal(elements['step-completed'].attributes['aria-current'], 'step');
  assert.equal(elements['step-processing'].attributes['aria-current'], undefined);
});

test('opens a completed job by id after a new tab', async () => {
  const { elements, values } = setup(async (url) => {
    assert.equal(url, `/cloud/jobs/${jobId}`);
    return { ok: true, json: async () => ({ status: 'COMPLETED' }) };
  });
  elements['lookup-id'].value = jobId;
  await elements['lookup-form'].submit({ preventDefault() {} });
  assert.equal(values.get('report.cloudJobId'), jobId);
  assert.equal(elements.report.href, `/cloud/jobs/${jobId}/report`);
});

test('does not start a worker without confirmation', async () => {
  const calls = [];
  const { elements } = setup(async (url) => {
    calls.push(url);
    return { ok: true, json: async () => ({ status: 'QUEUED' }) };
  }, () => false);
  elements['job-file'].files = [{ arrayBuffer: async () => Buffer.from('csv') }];
  await elements['job-form'].submit({ preventDefault() {} });
  await elements['start-worker'].click();
  assert.equal(calls.includes('/cloud/start-worker'), false);
});

test('surfaces an uncertain submission and preserves its job id', async () => {
  const { elements, values } = setup(async () => ({
    ok: false, json: async () => ({ error: 'SQS unavailable' }),
  }));
  elements['job-file'].files = [{ arrayBuffer: async () => Buffer.from('csv') }];
  await elements['job-form'].submit({ preventDefault() {} });
  assert.equal(values.get('report.cloudJobId'), jobId);
  assert.match(elements.error.textContent, /SQS unavailable/);
  assert.match(elements.error.textContent, /check its AWS state/);
  assert.equal(elements['start-worker'].hidden, true);
});

test('failed jobs show the failure without highlighting a progress step', async () => {
  const { elements } = setup(async () => ({
    ok: true, json: async () => ({ status: 'FAILED', error: 'Invalid CSV' }),
  }));
  elements['lookup-id'].value = jobId;
  await elements['lookup-form'].submit({ preventDefault() {} });
  assert.equal(elements['job-status'].textContent, 'FAILED');
  assert.equal(elements.error.textContent, 'Invalid CSV');
  assert.equal(elements['step-processing'].classList.contains('current'), false);
  assert.equal(elements.report.hidden, true);
});

test('polls active jobs and retries transient status errors', async () => {
  let count = 0;
  const { elements, timers } = setup(async () => {
    count++;
    if (count === 2) throw new Error('network error');
    return { ok: true, json: async () => ({ status: count === 1 ? 'QUEUED' : 'PROCESSING' }) };
  });
  elements['lookup-id'].value = jobId;
  await elements['lookup-form'].submit({ preventDefault() {} });
  assert.equal(timers[0].delay, 5000);
  await timers[0].callback();
  assert.equal(elements['job-status'].textContent, 'Unknown');
  assert.match(elements.error.textContent, /Retrying automatically/);
  assert.equal(timers[1].delay, 10000);
  await timers[1].callback();
  assert.equal(elements['job-status'].textContent, 'PROCESSING');
  assert.equal(elements.error.textContent, '');
  assert.equal(timers[2].delay, 5000);
});
