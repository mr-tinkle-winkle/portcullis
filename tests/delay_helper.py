"""Runs real DelayQueue threads inside the current network namespace for a few seconds
(used by test_kernel.py through `nsenter --net=...`)."""
import json
import sys
import threading
import time

from portcullis.delay import DelayQueue

seconds = float(sys.argv[1])
specs = [(int(a.split(":")[0]), int(a.split(":")[1])) for a in sys.argv[2:]]      # qnum:delay_ms
queues = [DelayQueue(q, (lambda d=d: d)) for q, d in specs]
threads = [threading.Thread(target=q.run) for q in queues]
for t in threads:
    t.start()
time.sleep(seconds)
for q in queues:
    q.stop()
for t in threads:
    t.join(3)
print(json.dumps({"released": {q.qnum: q.released for q in queues}, "errors": [q.error for q in queues if q.error]}))
