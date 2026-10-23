#!/usr/bin/env python3
import importlib.util
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[5]
spec = importlib.util.spec_from_file_location('a2agent', root/'docker/harness/aos_agent.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
mode = os.environ.get('A2_MODE', 'normal')
if mode == 'exit-failure': sys.exit(7)
if mode == 'parse-failure':
    print('not json')
    sys.exit(0)
class Bus:
    def __init__(self, *args): pass
    def inbox(self):
        body = '😀' * 8000 if mode == 'large' else 'ordinary operator question'
        return {'pending_requests': [dict(id=f'operator-i{i}', task_id='operator', kind='request', reply_to=None,
                     body=body, framed=f'<operator-message>{body}</operator-message>')
                     for i in range(20 if mode == 'large' else 1)]}
    def ack(self, ident): pass
a.Bus = Bus
if mode == 'inbox-write-failure' and sys.argv[1] == 'inbox' or mode == 'stop-write-failure' and sys.argv[1] == 'stop':
    def failed_send(message): raise OSError('probe output write failed')
    a.send = failed_send
sys.exit(a.main(sys.argv[1:]))
