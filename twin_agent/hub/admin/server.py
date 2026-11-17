"""A threaded, time-limited WSGI server (stdlib ``wsgiref``).

The stock ``wsgiref`` server is single-threaded with no socket timeout: one slow client (or an open browser
connection) stalls every other request. This one:
* serves each request on its own DAEMON thread, so shutdown never waits on a stuck client and the process can exit;
* caps concurrent requests (``max_threads``); beyond the cap the connection is closed at once;
* sets a socket timeout (``timeout`` seconds) on every connection, which bounds slow headers and slow bodies.
"""
from __future__ import annotations

import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

REQUEST_TIMEOUT = 15.0
MAX_THREADS = 32


class TimeoutHandler(WSGIRequestHandler):
    timeout = REQUEST_TIMEOUT

    def log_message(self, fmt, *args):          # no request lines (they carry paths and IPs)
        pass


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    block_on_close = False
    max_threads = MAX_THREADS

    def __init__(self, *a, **kw):
        self._slots = threading.BoundedSemaphore(self.max_threads)
        super().__init__(*a, **kw)

    def handle_error(self, request, client_address):
        """A timed-out or broken connection is routine here: say nothing (the default prints a traceback and the peer)."""

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.close_request(request)                 # over the cap: refuse, never queue without bound
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_threaded_server(host: str, port: int, app, *, timeout: float = REQUEST_TIMEOUT):
    handler = type("Handler", (TimeoutHandler,), {"timeout": timeout})
    return make_server(host, port, app, server_class=ThreadedWSGIServer, handler_class=handler)
