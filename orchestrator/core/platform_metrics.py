from __future__ import annotations

import math
import os
import resource
import shutil
import threading
import time
from collections import defaultdict
from dataclasses import dataclass

from orchestrator.storage.db import create_db_engine

REQUEST_DURATION_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


@dataclass
class _HistogramState:
    bucket_counts: list[int]
    count: int = 0
    sum_seconds: float = 0.0


class PlatformMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process_start_time_seconds = time.time()
        self._api_requests_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._api_request_errors_total: dict[tuple[str, str, str], int] = defaultdict(int)
        self._api_request_duration: dict[tuple[str, str], _HistogramState] = {}
        self._worker_failures_total: dict[str, int] = defaultdict(int)

    def reset(self) -> None:
        with self._lock:
            self._process_start_time_seconds = time.time()
            self._api_requests_total.clear()
            self._api_request_errors_total.clear()
            self._api_request_duration.clear()
            self._worker_failures_total.clear()

    def record_api_request(self, *, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        normalized_method = _normalize_method(method)
        normalized_route = _normalize_route(route)
        status_class = _status_class(status_code)
        with self._lock:
            self._api_requests_total[(normalized_method, normalized_route, status_class)] += 1
            histogram_key = (normalized_method, normalized_route)
            histogram = self._api_request_duration.get(histogram_key)
            if histogram is None:
                histogram = _HistogramState(bucket_counts=[0 for _ in REQUEST_DURATION_BUCKETS_SECONDS])
                self._api_request_duration[histogram_key] = histogram
            normalized_duration = max(0.0, duration_seconds)
            histogram.count += 1
            histogram.sum_seconds += normalized_duration
            for index, bucket in enumerate(REQUEST_DURATION_BUCKETS_SECONDS):
                if normalized_duration <= bucket:
                    histogram.bucket_counts[index] += 1
                    break
            if status_code >= 500:
                self._api_request_errors_total[(normalized_method, normalized_route, "http_5xx")] += 1

    def record_api_exception(self, *, method: str, route: str, error_type: str) -> None:
        normalized_method = _normalize_method(method)
        normalized_route = _normalize_route(route)
        normalized_error_type = _normalize_error_type(error_type)
        with self._lock:
            self._api_request_errors_total[(normalized_method, normalized_route, normalized_error_type)] += 1

    def record_worker_failure(self, *, kind: str) -> None:
        normalized_kind = _normalize_failure_kind(kind)
        with self._lock:
            self._worker_failures_total[normalized_kind] += 1

    def render_prometheus(self) -> str:
        runtime = _runtime_metrics_snapshot(process_start_time_seconds=self._process_start_time_seconds)
        with self._lock:
            api_requests = dict(self._api_requests_total)
            api_request_errors = dict(self._api_request_errors_total)
            api_request_duration = dict(self._api_request_duration)
            worker_failures = dict(self._worker_failures_total)

        lines: list[str] = []
        lines.extend(
            [
                "# HELP master_builder_process_start_time_seconds Unix timestamp for current process start time.",
                "# TYPE master_builder_process_start_time_seconds gauge",
                f"master_builder_process_start_time_seconds {runtime['process_start_time_seconds']}",
                "# HELP master_builder_process_uptime_seconds Process uptime in seconds.",
                "# TYPE master_builder_process_uptime_seconds gauge",
                f"master_builder_process_uptime_seconds {runtime['process_uptime_seconds']}",
                "# HELP master_builder_process_cpu_load_ratio Current 1m load average normalized by CPU count.",
                "# TYPE master_builder_process_cpu_load_ratio gauge",
                f"master_builder_process_cpu_load_ratio {runtime['process_cpu_load_ratio']}",
                "# HELP master_builder_process_memory_bytes Resident memory usage in bytes.",
                "# TYPE master_builder_process_memory_bytes gauge",
                f"master_builder_process_memory_bytes {runtime['process_memory_bytes']}",
                "# HELP master_builder_disk_total_bytes Filesystem total bytes.",
                "# TYPE master_builder_disk_total_bytes gauge",
                f"master_builder_disk_total_bytes {runtime['disk_total_bytes']}",
                "# HELP master_builder_disk_used_bytes Filesystem used bytes.",
                "# TYPE master_builder_disk_used_bytes gauge",
                f"master_builder_disk_used_bytes {runtime['disk_used_bytes']}",
                "# HELP master_builder_disk_free_bytes Filesystem free bytes.",
                "# TYPE master_builder_disk_free_bytes gauge",
                f"master_builder_disk_free_bytes {runtime['disk_free_bytes']}",
                "# HELP master_builder_db_pool_size SQLAlchemy connection pool size.",
                "# TYPE master_builder_db_pool_size gauge",
                f"master_builder_db_pool_size {runtime['db_pool_size']}",
                "# HELP master_builder_db_pool_checked_out SQLAlchemy checked-out connections.",
                "# TYPE master_builder_db_pool_checked_out gauge",
                f"master_builder_db_pool_checked_out {runtime['db_pool_checked_out']}",
                "# HELP master_builder_db_pool_overflow SQLAlchemy pool overflow count.",
                "# TYPE master_builder_db_pool_overflow gauge",
                f"master_builder_db_pool_overflow {runtime['db_pool_overflow']}",
                "# HELP master_builder_db_pool_saturation_ratio SQLAlchemy checked-out to capacity ratio.",
                "# TYPE master_builder_db_pool_saturation_ratio gauge",
                f"master_builder_db_pool_saturation_ratio {runtime['db_pool_saturation_ratio']}",
                "# HELP master_builder_api_requests_total Total API requests by method, route, and status class.",
                "# TYPE master_builder_api_requests_total counter",
            ]
        )
        for (method, route, status_class), value in sorted(api_requests.items()):
            lines.append(
                "master_builder_api_requests_total"
                f'{{method="{_label_escape(method)}",route="{_label_escape(route)}",status_class="{_label_escape(status_class)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP master_builder_api_request_errors_total Total API request errors by method, route, and error type.",
                "# TYPE master_builder_api_request_errors_total counter",
            ]
        )
        for (method, route, error_type), value in sorted(api_request_errors.items()):
            lines.append(
                "master_builder_api_request_errors_total"
                f'{{method="{_label_escape(method)}",route="{_label_escape(route)}",error_type="{_label_escape(error_type)}"}} {value}'
            )

        lines.extend(
            [
                "# HELP master_builder_api_request_duration_seconds API request duration histogram.",
                "# TYPE master_builder_api_request_duration_seconds histogram",
            ]
        )
        for (method, route), histogram in sorted(api_request_duration.items()):
            running = 0
            for index, bucket in enumerate(REQUEST_DURATION_BUCKETS_SECONDS):
                running += histogram.bucket_counts[index]
                lines.append(
                    "master_builder_api_request_duration_seconds_bucket"
                    f'{{method="{_label_escape(method)}",route="{_label_escape(route)}",le="{bucket}"}} {running}'
                )
            lines.append(
                "master_builder_api_request_duration_seconds_bucket"
                f'{{method="{_label_escape(method)}",route="{_label_escape(route)}",le="+Inf"}} {histogram.count}'
            )
            lines.append(
                "master_builder_api_request_duration_seconds_sum"
                f'{{method="{_label_escape(method)}",route="{_label_escape(route)}"}} {round(histogram.sum_seconds, 6)}'
            )
            lines.append(
                "master_builder_api_request_duration_seconds_count"
                f'{{method="{_label_escape(method)}",route="{_label_escape(route)}"}} {histogram.count}'
            )

        total_requests = sum(api_requests.values())
        total_errors = sum(api_request_errors.values())
        error_rate = 0.0 if total_requests <= 0 else total_errors / total_requests
        lines.extend(
            [
                "# HELP master_builder_api_error_rate_ratio API error rate ratio (errors/requests).",
                "# TYPE master_builder_api_error_rate_ratio gauge",
                f"master_builder_api_error_rate_ratio {round(error_rate, 6)}",
                "# HELP master_builder_worker_failures_total Worker failures by kind.",
                "# TYPE master_builder_worker_failures_total counter",
            ]
        )
        for kind, value in sorted(worker_failures.items()):
            lines.append(f'master_builder_worker_failures_total{{kind="{_label_escape(kind)}"}} {value}')

        lines.append("")
        return "\n".join(lines)


def _normalize_method(method: str) -> str:
    normalized = str(method or "").strip().upper()
    return normalized or "UNKNOWN"


def _normalize_route(route: str) -> str:
    normalized = str(route or "").strip()
    return normalized or "__unmatched__"


def _normalize_error_type(error_type: str) -> str:
    normalized = str(error_type or "").strip().lower()
    return normalized or "unknown"


def _normalize_failure_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"crash", "dependency"}:
        return normalized
    return "unknown"


def _status_class(status_code: int) -> str:
    try:
        parsed = int(status_code)
    except (TypeError, ValueError):
        return "unknown"
    return f"{parsed // 100}xx"


def _label_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _process_memory_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # Linux reports ru_maxrss in KB, macOS reports bytes.
    rss = int(getattr(usage, "ru_maxrss", 0) or 0)
    if rss <= 0:
        return 0
    if os.uname().sysname.lower() == "darwin":
        return rss
    return rss * 1024


def _process_cpu_load_ratio() -> float:
    cpu_count = os.cpu_count() or 1
    if cpu_count <= 0:
        return 0.0
    try:
        load_one = os.getloadavg()[0]
    except OSError:
        return 0.0
    return round(max(0.0, float(load_one)) / float(cpu_count), 6)


def _db_pool_snapshot() -> dict[str, float]:
    engine = create_db_engine()
    pool = engine.pool
    size = 0.0
    checked_out = 0.0
    overflow = 0.0

    size_fn = getattr(pool, "size", None)
    if callable(size_fn):
        try:
            size = float(size_fn())
        except Exception:
            size = 0.0

    checked_out_fn = getattr(pool, "checkedout", None)
    if callable(checked_out_fn):
        try:
            checked_out = float(checked_out_fn())
        except Exception:
            checked_out = 0.0

    overflow_fn = getattr(pool, "overflow", None)
    if callable(overflow_fn):
        try:
            overflow = float(overflow_fn())
        except Exception:
            overflow = 0.0

    capacity = size + max(0.0, overflow)
    saturation = 0.0
    if capacity > 0:
        saturation = checked_out / capacity
    return {
        "db_pool_size": size,
        "db_pool_checked_out": checked_out,
        "db_pool_overflow": overflow,
        "db_pool_saturation_ratio": round(max(0.0, saturation), 6),
    }


def _runtime_metrics_snapshot(*, process_start_time_seconds: float) -> dict[str, float]:
    now = time.time()
    disk = shutil.disk_usage("/")
    snapshot = {
        "process_start_time_seconds": round(process_start_time_seconds, 3),
        "process_uptime_seconds": round(max(0.0, now - process_start_time_seconds), 3),
        "process_cpu_load_ratio": _process_cpu_load_ratio(),
        "process_memory_bytes": float(_process_memory_bytes()),
        "disk_total_bytes": float(disk.total),
        "disk_used_bytes": float(disk.used),
        "disk_free_bytes": float(disk.free),
    }
    snapshot.update(_db_pool_snapshot())
    for key, value in list(snapshot.items()):
        if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
            snapshot[key] = 0.0
    return snapshot


platform_metrics = PlatformMetrics()


def reset_platform_metrics_for_tests() -> None:
    platform_metrics.reset()
