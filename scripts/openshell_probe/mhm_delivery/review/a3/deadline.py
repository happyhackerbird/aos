"""Exercise D32's literal timeout through the actual Bus.post and HTTP reader."""
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import sys
import threading
import time

sys.dont_write_bytecode = True
root = Path(__file__).resolve().parents[5]
spec = importlib.util.spec_from_file_location('a3agent', root / 'docker/harness/aos_agent.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
budget = 2
start = time.monotonic()
deadline = start + budget
timeout = max(0.5, min(a.WAIT_CALL_TIMEOUT, deadline - time.monotonic() - 0.5))
client, server = socket.socketpair()
body = b'{"intent":{"state":"stored"}}'
def writer():
    with server:
        server.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n')
        for char in body:
            server.sendall(bytes([char]))
            time.sleep(0.10)
thread = threading.Thread(target=writer)
thread.start()
def open_response(request, timeout):
    client.settimeout(timeout)
    response = http.client.HTTPResponse(client)
    response.begin()
    return response
a.urllib.request.urlopen = open_response
bus = a.Bus({'bus_url': 'http://probe.invalid'}, timeout)
result = bus.post('/probe', {}, timeout=timeout)
elapsed = time.monotonic() - start
thread.join()
client.close()
print(json.dumps({'probe': 'D32 actual HTTP body read', 'budget': budget, 'D32_timeout': timeout,
                  'elapsed': round(elapsed, 3), 'exceeded_budget': elapsed > budget, 'result': result}))
# The floor remains positive after a deadline; execute that exact expression as well.
now = 899.75
timeouts = []
for _ in range(3):
    limit = max(0.5, min(30, 900 - now - 0.5))
    timeouts.append(limit)
    now += limit
print(json.dumps({'probe': 'D32 ack timeout expression only', 'answer_at': 899.75,
                  'three_ack_timeouts': timeouts, 'end': now,
                  'reach': 'literal arithmetic; whether late acks are skipped is not specified'}))
