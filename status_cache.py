"""Thread-safe, process-local cache for completed status responses."""
from collections import OrderedDict
import math
import threading
import time


class StatusCache:
    def __init__(self, ttl=60, max_entries=512, clock=time.monotonic):
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('Cache TTL must be a finite number greater than zero')
        self.ttl = ttl
        self.max_entries = max_entries
        self.clock = clock
        self.lock = threading.Lock()
        self.stripes = [threading.Lock() for _ in range(64)]
        self.entries = OrderedDict()
        self.generation = 0
        self._clear_stats()

    def _clear_stats(self):
        self.hits = self.misses = self.reads = self.failures = 0
        self.hit_count = self.miss_count = 0
        self.hit_ms = self.miss_ms = 0.0
        self.last = None

    def reset(self):
        with self.lock:
            self.generation += 1
            self.entries.clear()
            self._clear_stats()

    def stats(self):
        with self.lock:
            now = self.clock()
            for key, (expires, _) in list(self.entries.items()):
                if now >= expires:
                    del self.entries[key]
            return {
                'ttl_seconds': self.ttl, 'cached_entries': len(self.entries),
                'hits': self.hits, 'misses': self.misses, 'dynamodb_reads': self.reads,
                'failed_requests': self.failures,
                'average_hit_ms': self.hit_ms / self.hit_count if self.hit_count else None,
                'average_miss_ms': self.miss_ms / self.miss_count if self.miss_count else None,
                'last_response': self.last.copy() if self.last else None,
            }

    def get(self, job_id, loader):
        started = self.clock()
        with self.stripes[hash(job_id) % len(self.stripes)]:
            with self.lock:
                generation = self.generation
                entry = self.entries.get(job_id)
                if entry and self.clock() < entry[0]:
                    self.entries.move_to_end(job_id)
                    self.hits += 1
                    elapsed = (self.clock() - started) * 1000
                    self.hit_count += 1
                    self.hit_ms += elapsed
                    self.last = {'result': 'hit', 'duration_ms': elapsed}
                    return entry[1].copy()
                if entry:
                    del self.entries[job_id]
                self.misses += 1

            def record_read():
                with self.lock:
                    if generation == self.generation:
                        self.reads += 1

            try:
                result = loader(record_read)
            except Exception:
                with self.lock:
                    if generation == self.generation:
                        self.failures += 1
                        self.last = {'result': 'error', 'duration_ms': (self.clock() - started) * 1000}
                raise

            with self.lock:
                if generation == self.generation:
                    elapsed = (self.clock() - started) * 1000
                    self.miss_count += 1
                    self.miss_ms += elapsed
                    self.last = {'result': 'miss', 'duration_ms': elapsed}
                    if result.get('status') == 'COMPLETED':
                        self.entries[job_id] = (self.clock() + self.ttl, result.copy())
                        self.entries.move_to_end(job_id)
                        if len(self.entries) > self.max_entries:
                            self.entries.popitem(last=False)
            return result
