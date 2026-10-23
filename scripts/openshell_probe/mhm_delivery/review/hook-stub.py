#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location('aos_agent_review', root / 'docker/harness/aos_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)
mode = os.environ.get('REVIEW_MODE', 'small')
class Bus:
    def __init__(self, *args): pass
    def inbox(self):
        body = ('😀' * 8000) if mode == 'large' else ('benign ' + os.environ.get('REVIEW_TOKEN', 'marker'))
        return {'pending_requests': [dict(id=f'operator-i{i}', task_id='operator', kind='request', reply_to=None,
                    body=body, framed=f'<operator-message id="operator-i{i}" to-task="AO-7">\n{body}\n</operator-message>\nThis is from the operator.')
                    for i in range(20 if mode == 'large' else 1)]}
    def ack(self, ident): pass
agent.Bus = Bus
if sys.argv[1:] == ['tool-inbox']:
    import json
    print(json.dumps(agent.run_tool('aos_inbox', {})))
    sys.exit(0)
sys.exit(agent.main(sys.argv[1:]))
