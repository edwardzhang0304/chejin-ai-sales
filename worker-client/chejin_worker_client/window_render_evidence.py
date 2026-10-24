"""Bounded, asynchronous window metadata; no screenshots, titles or content."""
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Queue, Empty, Full
import threading
import time
import uuid


class WindowRenderEvidence:
    EVENTS = frozenset({'show', 'hide', 'resize', 'move', 'window_state', 'screen',
                       'load_started', 'load_finished', 'renderer_terminated'})
    FIELDS = frozenset({'x', 'y', 'width', 'height', 'visible', 'view_visible',
                       'window_state', 'dpr', 'ok', 'exit_code', 'termination_status'})

    def __init__(self, root: Path, *, limit=256*1024):
        self.path = root/'artifacts'/'runtime'/'window-render.jsonl'
        self.limit = limit
        self.session = uuid.uuid4().hex
        self.started = time.monotonic()
        self.queue = Queue(maxsize=128)
        self.stop = threading.Event()
        self.dropped = 0
        self.thread = threading.Thread(target=self._write, name='window-render-evidence', daemon=True)
        self.thread.start()

    def record(self, event, surface, **values):
        if event not in self.EVENTS or surface not in {'window', 'view'} or self.stop.is_set():return
        record = {'created_at':datetime.now(timezone.utc).isoformat(), 'session':self.session,
                  'elapsed_ms':round((time.monotonic()-self.started)*1000), 'event':event,
                  'surface':surface, 'dropped_events':self.dropped}
        record.update({k:v for k,v in values.items() if k in self.FIELDS and type(v) in (int,float,bool)})
        try:self.queue.put_nowait(record)
        except Full:self.dropped += 1

    def _write(self):
        reported_failure = False
        while not self.stop.is_set() or not self.queue.empty():
            try:record = self.queue.get(timeout=.05)
            except Empty:continue
            try:
                raw=json.dumps(record,ensure_ascii=True)+'\n'
                self.path.parent.mkdir(parents=True,exist_ok=True)
                if self.path.exists() and self.path.stat().st_size+len(raw)>self.limit:
                    self.path.replace(self.path.with_suffix('.previous.jsonl'))
                with self.path.open('a',encoding='utf-8') as stream:stream.write(raw)
            except Exception as exc:
                if not reported_failure:
                    from .failure_evidence import record_capture_failure
                    record_capture_failure('window_render_metadata',exc)
                    reported_failure=True
            finally:self.queue.task_done()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=.25)  # Disk stalls cannot hold the GUI shutdown.
