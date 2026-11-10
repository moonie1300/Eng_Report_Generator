import concurrent.futures
import threading
import unittest

from status_cache import StatusCache


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class StatusCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = StatusCache(ttl=60, clock=self.clock)
        self.calls = []

    def loader(self, job, status='COMPLETED'):
        def load(record_read):
            record_read()
            self.calls.append(job)
            self.clock.value += 0.01
            return {'job_id': job, 'status': status}
        return load

    def test_completed_hit_and_exact_expiry(self):
        self.assertEqual(self.cache.get('a', self.loader('a'))['status'], 'COMPLETED')
        self.clock.value += 59
        self.cache.get('a', self.loader('a'))
        self.assertEqual(self.calls, ['a'])
        self.clock.value = 60.01
        self.cache.get('a', self.loader('a'))
        stats = self.cache.stats()
        self.assertEqual((stats['hits'], stats['misses'], stats['dynamodb_reads']), (1, 2, 2))
        self.assertAlmostEqual(stats['average_miss_ms'], 10.0)
        self.assertEqual(stats['last_response']['result'], 'miss')

    def test_job_isolation_and_copy(self):
        first = self.cache.get('a', self.loader('a'))
        first['status'] = 'FAILED'
        self.cache.get('b', self.loader('b'))
        self.assertEqual(self.cache.get('a', self.loader('a'))['status'], 'COMPLETED')
        self.assertEqual(self.calls, ['a', 'b'])
        self.assertEqual(self.cache.stats()['cached_entries'], 2)

    def test_active_failed_missing_and_errors_are_never_cached(self):
        for status in ('QUEUED', 'PROCESSING', 'FAILED'):
            for _ in range(2):
                self.cache.get(status, self.loader(status, status))
        def missing(record_read):
            record_read()
            raise LookupError('not found')
        for _ in range(2):
            with self.assertRaises(LookupError):
                self.cache.get('missing', missing)
        stats = self.cache.stats()
        self.assertEqual((stats['hits'], stats['misses'], stats['dynamodb_reads']), (0, 8, 8))
        self.assertEqual(stats['failed_requests'], 2)
        self.assertEqual(stats['cached_entries'], 0)

    def test_concurrent_same_job_loads_once(self):
        cache = StatusCache()
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls = []

        def load(record_read):
            record_read()
            with lock:
                calls.append(1)
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release loader')
            return {'status': 'COMPLETED'}

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(cache.get, 'same', load) for _ in range(12)]
            self.assertTrue(entered.wait(5))
            release.set()
            for future in futures:
                self.assertEqual(future.result(timeout=5)['status'], 'COMPLETED')
        stats = cache.stats()
        self.assertEqual((len(calls), stats['hits'], stats['misses'], stats['dynamodb_reads']), (1, 11, 1, 1))

    def test_reset_during_inflight_read_does_not_restore_stale_entry_or_counts(self):
        cache = StatusCache()
        entered, release = threading.Event(), threading.Event()

        def load(record_read):
            record_read()
            entered.set()
            if not release.wait(5):
                raise TimeoutError('test did not release loader')
            return {'status': 'COMPLETED'}

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(cache.get, 'same', load)
            self.assertTrue(entered.wait(5))
            cache.reset()
            release.set()
            self.assertEqual(future.result(timeout=5)['status'], 'COMPLETED')
        stats = cache.stats()
        self.assertEqual((stats['cached_entries'], stats['hits'], stats['misses'], stats['dynamodb_reads']), (0, 0, 0, 0))

    def test_capacity_and_invalid_ttl(self):
        cache = StatusCache(ttl=60, max_entries=1, clock=self.clock)
        cache.get('a', self.loader('a'))
        cache.get('b', self.loader('b'))
        cache.get('a', self.loader('a'))
        self.assertEqual(cache.stats()['dynamodb_reads'], 3)
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StatusCache(ttl=value)


if __name__ == '__main__':
    unittest.main()
