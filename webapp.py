"""Simple local web interface for generating battery test reports."""
import argparse
import base64
import json
import logging
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.report import build_report
from core.limits import optional_limit
from status_cache import StatusCache

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / 'static'
PAGE = (STATIC_DIR / 'index.html').read_bytes()
LOGGER = logging.getLogger(__name__)
STATUS_CACHE = StatusCache()


def generate(payload):
    csv_text = payload.get('csv')
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError('Choose a CSV file first')
    name = Path(str(payload.get('filename') or 'upload.csv')).name
    max_voltage = optional_limit(payload.get('max_voltage'))
    max_abs_current = optional_limit(payload.get('max_abs_current'))
    with tempfile.TemporaryDirectory() as folder:
        csv_path = Path(folder) / 'input.csv'
        pdf_path = Path(folder) / 'report.pdf'
        csv_path.write_text(csv_text, encoding='utf-8')
        metrics = build_report(csv_path, pdf_path, name, max_voltage, max_abs_current)
        pdf = base64.b64encode(pdf_path.read_bytes()).decode('ascii')
    return {'metrics': metrics, 'pdf': pdf}


class Handler(BaseHTTPRequestHandler):
    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_body(status, 'application/json', json.dumps(data).encode('utf-8'))

    def do_GET(self):
        if self.path in ('/', '/cloud'):
            self.send_body(200, 'text/html; charset=utf-8', (STATIC_DIR / 'cloud-jobs.html').read_bytes())
        elif self.path == '/local':
            self.send_body(200, 'text/html; charset=utf-8', PAGE)
        elif self.path == '/static/cloud-jobs.js':
            self.send_body(200, 'text/javascript; charset=utf-8', (STATIC_DIR / 'cloud-jobs.js').read_bytes())
        elif self.path == '/static/cache-stats.js':
            self.send_body(200, 'text/javascript; charset=utf-8', (STATIC_DIR / 'cache-stats.js').read_bytes())
        elif self.path == '/static/cloud-upload.js':
            try:
                body = (STATIC_DIR / 'cloud-upload.js').read_bytes()
            except OSError as exc:
                self.send_json(500, {'error': f'Cannot read cloud upload script: {exc}'})
                return
            self.send_body(200, 'text/javascript; charset=utf-8', body)
        elif self.path == '/cloud/cache':
            if not self.local_request():
                self.send_json(403, {'error': 'Local access only'})
                return
            self.send_json(200, STATUS_CACHE.stats())
        elif self.path.startswith('/cloud/jobs/'):
            if not self.local_request():
                self.send_json(403, {'error': 'Local access only'})
                return
            parts = self.path.split('/')
            if len(parts) not in (4, 5) or (len(parts) == 5 and parts[4] != 'report'):
                self.send_json(404, {'error': 'Not found'})
                return
            self.cloud_request('report' if len(parts) == 5 else 'status', parts[3])
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path not in ('/report', '/cloud/jobs', '/cloud/start-worker', '/cloud/cache/reset'):
            self.send_json(404, {'error': 'Not found'})
            return
        if self.path != '/report' and (
            not self.local_request()
            or self.headers.get('X-Local-Report') != '1'
            or self.headers.get_content_type() != 'application/json'
        ):
            self.send_json(403, {'error': 'Local requests only'})
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                raise ValueError('Upload must be between 1 byte and 10 MB')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request')
            if self.path == '/report':
                self.send_json(200, generate(payload))
            elif self.path == '/cloud/cache/reset':
                STATUS_CACHE.reset()
                self.send_json(200, STATUS_CACHE.stats())
            else:
                self.cloud_request('submit' if self.path == '/cloud/jobs' else 'start_worker',
                                   payload.get('job_id'), payload)
        except (ValueError, OSError, UnicodeError) as exc:
            self.send_json(400, {'error': str(exc)})

    def local_request(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        origin = self.headers.get('Origin')
        return (self.headers.get('Host') == expected
                and (origin is None or origin == f'http://{expected}'))

    def cloud_request(self, action, job_id, payload=None):
        try:
            from cloud_jobs import CloudJobs
            if action == 'status':
                CloudJobs.check_id(job_id)

                def load_status(record_read):
                    cloud = CloudJobs()
                    record_read()
                    return cloud.status(job_id)

                result = STATUS_CACHE.get(job_id, load_status)
            elif action == 'submit':
                cloud = CloudJobs()
                result = cloud.submit(job_id, payload.get('csv'),
                                      payload.get('max_voltage'), payload.get('max_abs_current'))
            elif action == 'start_worker':
                result = CloudJobs().start_worker(job_id)
            elif action == 'report':
                self.send_body(200, 'application/pdf', CloudJobs().report(job_id))
                return
            self.send_json(200, result)
        except ImportError as exc:
            LOGGER.exception('Cloud dependencies are unavailable')
            self.send_json(503, {'error': f'Cloud dependencies unavailable: {exc}. Install requirements-dev.txt.'})
        except LookupError as exc:
            self.send_json(404, {'error': str(exc)})
        except ValueError as exc:
            self.send_json(400, {'error': str(exc)})
        except Exception as exc:
            LOGGER.exception('Cloud operation %s failed', action)
            self.send_json(502, {'error': f'AWS operation failed: {exc}'})

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--cache-ttl', type=float, default=60, help='Completed status cache TTL in seconds')
    args = parser.parse_args()
    global STATUS_CACHE
    try:
        STATUS_CACHE = StatusCache(ttl=args.cache_ttl)
    except ValueError as exc:
        parser.error(str(exc))
    # Bound to localhost only so uploaded data is not exposed to the network.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'Open http://127.0.0.1:{args.port} (Ctrl+C to stop)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
