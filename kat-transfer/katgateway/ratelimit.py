"""In-memory failed sign-in limit for a single gateway process.

Behind NGINX every request arrives from the proxy's address, so the per-client
limit belongs in NGINX (`limit_req`, see deploy/nginx.live.conf). This limits
password guessing against any one account.
"""
from collections import defaultdict, deque
import threading
import time


class FailedLoginLimit:
    def __init__(self, per_user=5, window_s=300.0, clock=time.monotonic):
        self.per_user = per_user
        self.window_s = window_s
        self.clock = clock
        self._failures = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, username, now):
        failures = self._failures[username.lower()]
        while failures and failures[0] <= now - self.window_s:
            failures.popleft()
        return failures

    def retry_after(self, username):
        """Seconds until another attempt is allowed, or 0 if allowed now."""
        now = self.clock()
        with self._lock:
            failures = self._recent(username, now)
            if not failures:
                del self._failures[username.lower()]
            if len(failures) < self.per_user:
                return 0
            return int(failures[0] + self.window_s - now) + 1

    def failed(self, username):
        with self._lock:
            now = self.clock()
            self._recent(username, now).append(now)

    def succeeded(self, username):
        with self._lock:
            self._failures.pop(username.lower(), None)
