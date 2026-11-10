"""ECS service task scale-in protection through the local agent endpoint."""
import json
import os
from urllib.request import Request, urlopen


class TaskProtection:
    def __init__(self):
        self.url = os.environ['ECS_AGENT_URI'].rstrip('/') + '/task-protection/v1/state'

    def set(self, enabled):
        data = {'ProtectionEnabled': enabled}
        if enabled:
            data['ExpiresInMinutes'] = 5
        request = Request(self.url, data=json.dumps(data).encode(), method='PUT',
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
        if result.get('protection', {}).get('ProtectionEnabled') is not enabled:
            raise RuntimeError(f'ECS task protection failed: {result}')
        return result['protection'].get('TaskArn', 'unknown')
