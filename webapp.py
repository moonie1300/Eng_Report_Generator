"""Simple local web interface for generating battery test reports."""
import argparse
import base64
import json
import math
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from report import build_report

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
PAGE = (Path(__file__).parent / 'static' / 'index.html').read_bytes()


def optional_limit(value):
    if value in (None, ''):
        return None
    limit = float(value)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError('Limits must be numbers greater than zero')
    return limit


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
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_body(status, 'application/json', json.dumps(data).encode('utf-8'))

    def do_GET(self):
        if self.path == '/':
            self.send_body(200, 'text/html; charset=utf-8', PAGE)
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path != '/report':
            self.send_json(404, {'error': 'Not found'})
            return
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                raise ValueError('Upload must be between 1 byte and 10 MB')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('Invalid request')
            self.send_json(200, generate(payload))
        except (ValueError, OSError, UnicodeError) as exc:
            self.send_json(400, {'error': str(exc)})

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
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
