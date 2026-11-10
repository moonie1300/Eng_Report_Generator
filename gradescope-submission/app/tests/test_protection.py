import io
import json
import unittest
from unittest.mock import patch

from worker.protection import TaskProtection


class ProtectionTests(unittest.TestCase):
    def test_enable_and_disable_protection(self):
        with patch.dict('os.environ', {'ECS_AGENT_URI': 'http://127.0.0.1:1234'}):
            protection = TaskProtection()
        for enabled in (True, False):
            response = io.BytesIO(json.dumps({'protection': {
                'ProtectionEnabled': enabled, 'TaskArn': 'task',
            }}).encode())
            with patch('worker.protection.urlopen', return_value=response) as urlopen:
                self.assertEqual(protection.set(enabled), 'task')
            request = urlopen.call_args.args[0]
            self.assertEqual(request.method, 'PUT')
            self.assertEqual(json.loads(request.data)['ProtectionEnabled'], enabled)

    def test_agent_error_prevents_unprotected_processing(self):
        with patch.dict('os.environ', {'ECS_AGENT_URI': 'http://127.0.0.1:1234'}):
            protection = TaskProtection()
        response = io.BytesIO(b'{"error":{"Code":"AccessDeniedException"}}')
        with patch('worker.protection.urlopen', return_value=response), self.assertRaisesRegex(RuntimeError, 'AccessDeniedException'):
            protection.set(True)


if __name__ == '__main__':
    unittest.main()
