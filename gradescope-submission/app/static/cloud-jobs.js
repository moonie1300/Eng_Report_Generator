'use strict';

(() => {
  const form = document.getElementById('job-form');
  const lookup = document.getElementById('lookup-form');
  const lookupId = document.getElementById('lookup-id');
  const input = document.getElementById('job-file');
  const maxVoltage = document.getElementById('max-voltage');
  const maxCurrent = document.getElementById('max-current');
  const submit = document.getElementById('submit-job');
  const job = document.getElementById('job');
  const jobId = document.getElementById('job-id');
  const status = document.getElementById('job-status');
  const error = document.getElementById('error');
  const start = document.getElementById('start-worker');
  const warning = document.getElementById('worker-warning');
  const refresh = document.getElementById('refresh');
  const task = document.getElementById('task');
  const report = document.getElementById('report');
  const steps = {
    QUEUED: document.getElementById('step-queued'),
    PROCESSING: document.getElementById('step-processing'),
    COMPLETED: document.getElementById('step-completed'),
  };
  let current = sessionStorage.getItem('report.cloudJobId');
  let timer;
  let requestNumber = 0;
  let activeJob = null;
  let statusError = false;

  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function showStatus(value) {
    status.textContent = value;
    for (const [name, element] of Object.entries(steps)) {
      element.classList.toggle('current', name === value);
      if (name === value) element.setAttribute('aria-current', 'step');
      else element.removeAttribute('aria-current');
    }
    start.hidden = value !== 'QUEUED';
    warning.hidden = start.hidden;
    report.hidden = value !== 'COMPLETED';
  }

  async function update() {
    if (!current) return;
    clearTimeout(timer);
    const requested = current;
    const requestId = ++requestNumber;
    job.hidden = false;
    jobId.textContent = requested;
    lookupId.value = requested;
    try {
      const data = await request(`/cloud/jobs/${encodeURIComponent(requested)}`);
      if (current !== requested || requestId !== requestNumber) return;
      if (statusError) error.textContent = '';
      statusError = false;
      showStatus(data.status);
      activeJob = (data.status === 'QUEUED' || data.status === 'PROCESSING') ? requested : null;
      if (data.status === 'COMPLETED') {
        report.href = `/cloud/jobs/${encodeURIComponent(current)}/report`;
        report.download = `report-${current}.pdf`;
      }
      if (data.status === 'FAILED') error.textContent = data.error || 'The worker reported a failure.';
      if (data.status === 'QUEUED' || data.status === 'PROCESSING') timer = setTimeout(update, 5000);
    } catch (err) {
      if (current !== requested || requestId !== requestNumber) return;
      showStatus('Unknown');
      statusError = true;
      if (activeJob === requested) {
        error.textContent = `Status check failed: ${err.message}. Retrying automatically.`;
        timer = setTimeout(update, 10000);
      } else {
        error.textContent = `Status check failed: ${err.message}. Use Refresh status to retry.`;
      }
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.textContent = '';
    const file = input.files[0];
    if (!file) return;
    submit.disabled = true;
    try {
      const csv = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
      clearTimeout(timer);
      requestNumber++;
      activeJob = null;
      current = crypto.randomUUID();
      sessionStorage.setItem('report.cloudJobId', current);
      job.hidden = false;
      jobId.textContent = current;
      showStatus('Submitting');
      task.textContent = '';
      await request('/cloud/jobs', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Report': '1' },
        body: JSON.stringify({
          job_id: current, csv, max_voltage: maxVoltage.value, max_abs_current: maxCurrent.value,
        }),
      });
      await update();
    } catch (err) {
      showStatus('Unknown');
      error.textContent = `Submission failed: ${err.message}. If a job ID is shown, check its AWS state before submitting again.`;
    } finally {
      submit.disabled = false;
    }
  });
  lookup.addEventListener('submit', (event) => {
    event.preventDefault();
    current = lookupId.value.trim();
    if (!current) return;
    activeJob = null;
    sessionStorage.setItem('report.cloudJobId', current);
    error.textContent = '';
    task.textContent = '';
    return update();
  });

  start.addEventListener('click', async () => {
    if (!confirm('Start one Fargate task? AWS charges may apply. It will process the next message in the queue.')) return;
    start.disabled = true;
    error.textContent = '';
    try {
      const data = await request('/cloud/start-worker', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Report': '1' },
        body: JSON.stringify({ job_id: current }),
      });
      task.textContent = `Worker task: ${data.task_arn}`;
      await update();
    } catch (err) {
      error.textContent = `Worker start failed: ${err.message}`;
    } finally {
      start.disabled = false;
    }
  });
  refresh.addEventListener('click', () => { error.textContent = ''; return update(); });
  if (current) update();
})();
