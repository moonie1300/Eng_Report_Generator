'use strict';

(() => {
  const stats = document.getElementById('cache-stats');
  const error = document.getElementById('cache-error');
  const refresh = document.getElementById('cache-refresh');
  const reset = document.getElementById('cache-reset');
  const format = (value) => value === null ? 'not available' : `${value.toFixed(2)} ms`;

  async function show(resetCache = false) {
    error.textContent = '';
    refresh.disabled = reset.disabled = true;
    try {
      const response = await fetch(resetCache ? '/cloud/cache/reset' : '/cloud/cache',
        resetCache ? {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Report': '1' },
          body: '{}',
        } : { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
      const last = data.last_response
        ? `${data.last_response.result}, ${format(data.last_response.duration_ms)}`
        : 'none';
      stats.textContent = `TTL: ${data.ttl_seconds} s | Cached entries: ${data.cached_entries} | ` +
        `Hits: ${data.hits} | Misses: ${data.misses} | DynamoDB reads: ${data.dynamodb_reads} | ` +
        `Failed requests: ${data.failed_requests} | Average hit: ${format(data.average_hit_ms)} | ` +
        `Average miss: ${format(data.average_miss_ms)} | Last response: ${last}`;
    } catch (err) {
      error.textContent = `Cache statistics unavailable: ${err.message}`;
    } finally {
      refresh.disabled = reset.disabled = false;
    }
  }
  refresh.addEventListener('click', () => show());
  reset.addEventListener('click', () => show(true));
})();
