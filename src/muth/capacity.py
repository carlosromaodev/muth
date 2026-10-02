"""Bound both uploads in flight and actual synchronous inference workers."""

import threading
from contextlib import contextmanager

from muth.errors import MuthError


class InferenceCapacity:
    def __init__(self, limit, metrics):
        self.limit, self.metrics = limit, metrics
        self.requests = 0
        self.workers = 0
        self.lock = threading.Lock()

    def acquire_request(self):
        with self.lock:
            if self.requests >= self.limit or self.workers >= self.limit:
                self.metrics.capacity_rejections.inc()
                return False
            self.requests += 1
            self.metrics.inference_requests.inc()
            return True

    def release_request(self):
        with self.lock:
            self.requests -= 1
            self.metrics.inference_requests.dec()

    @contextmanager
    def worker(self):
        with self.lock:
            if self.workers >= self.limit:
                self.metrics.capacity_rejections.inc()
                raise MuthError(
                    429, "inference_capacity_exceeded", "Capacidade de inferência ocupada."
                )
            self.workers += 1
            self.metrics.inference_workers.inc()
        try:
            yield
        finally:
            with self.lock:
                self.workers -= 1
                self.metrics.inference_workers.dec()
