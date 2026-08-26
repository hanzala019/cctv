"""
core.diagnostics

Per-worker and per-camera resource accounting.

What is and isn't measurable, because the difference matters:

CPU -- yes, per worker thread.
    The OS tracks CPU time per thread, and BackgroundWorker names every
    thread "{LOG_TAG}-{cam_id}", so a sample can be attributed to an
    exact subsystem and camera. This only works because of that naming;
    unnamed threads show up as Thread-7 and tell you nothing.

Memory -- NO, not per worker.
    Threads share one heap. There is no such thing as "the memory used
    by the motion thread", and any tool claiming otherwise is guessing.
    What IS measurable is the memory held by each camera's own objects
    -- frame buffers, zone mask caches, presence slots -- which is where
    essentially all of this app's per-camera memory actually lives. That
    is what per_camera_memory() reports, and it is a floor, not a total.

GPU -- only if inference is actually on a GPU provider.
    With onnxruntime on CPUExecutionProvider the answer is a flat zero,
    and reporting anything else would be theatre. gpu_report() says which
    provider is live and only queries nvidia-smi when there is a CUDA or
    TensorRT provider to query.

Usage:

    from core.diagnostics import ResourceMonitor

    monitor = ResourceMonitor()
    ...
    print(monitor.report(managers={
        "stream": stream_manager,
        "motion": motion_manager,
        "detect": detection_manager,
        "record": recording_manager,
    }))

Sampling is cheap (a few reads from the OS) but not free -- call it on
a timer of seconds, never from the frame poll loop.
"""

import threading
import time

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None


# ===================================================================
# CPU
# ===================================================================

class ResourceMonitor:
    """Samples per-thread CPU between calls.

    CPU time is cumulative since thread start, so a single reading tells
    you almost nothing useful. This keeps the previous sample and
    reports the delta, which is what you actually want: "what is this
    worker costing me right now".
    """

    def __init__(self):
        self._proc = psutil.Process() if psutil else None
        self._last = {}          # native_tid -> cumulative cpu seconds
        self._last_wall = None

    @property
    def available(self):
        return self._proc is not None

    def sample_cpu(self):
        """Returns {thread_name: percent_of_one_core} since the previous
        call. First call returns {} -- there is no delta yet."""
        if not self.available:
            return {}

        names = {t.native_id: t.name for t in threading.enumerate()}
        now_wall = time.time()
        current = {}
        for th in self._proc.threads():
            current[th.id] = th.user_time + th.system_time

        if self._last_wall is None:
            self._last, self._last_wall = current, now_wall
            return {}

        elapsed = now_wall - self._last_wall
        if elapsed <= 0:
            return {}

        out = {}
        for tid, cpu in current.items():
            delta = cpu - self._last.get(tid, cpu)
            name = names.get(tid, f"tid-{tid}")
            # Threads can share a name only if two workers use the same
            # LOG_TAG and cam_id, which would itself be a bug -- sum
            # anyway rather than silently dropping one.
            out[name] = out.get(name, 0.0) + (delta / elapsed) * 100.0

        self._last, self._last_wall = current, now_wall
        return out

    def cpu_by_camera(self):
        """Rolls the per-thread sample up per camera.

        Returns {cam_id: {"total": pct, "workers": {subsystem: pct}}}.
        Relies on the "{LOG_TAG}-{cam_id}" naming; threads that don't
        match are grouped under "_other".
        """
        by_cam = {}
        for name, pct in self.sample_cpu().items():
            if "-" in name:
                subsystem, cam_id = name.rsplit("-", 1)
            else:
                subsystem, cam_id = name, "_other"
            entry = by_cam.setdefault(cam_id, {"total": 0.0, "workers": {}})
            entry["total"] += pct
            entry["workers"][subsystem] = entry["workers"].get(subsystem, 0.0) + pct
        return by_cam

    # ----- process-wide ------------------------------------------------

    def process_memory_mb(self):
        """Resident set size for the whole process. The only memory
        number here that is a hard fact rather than an attribution."""
        if not self.available:
            return None
        return self._proc.memory_info().rss / (1024 * 1024)

    def thread_count(self):
        return threading.active_count()

    # ----- report --------------------------------------------------------

    def report(self, managers=None):
        """A human-readable snapshot. `managers` maps a short label to a
        WorkerManager, e.g. {"stream": stream_manager, ...}."""
        lines = []
        rss = self.process_memory_mb()
        lines.append(
            f"process: {self.thread_count()} threads"
            + (f", {rss:.0f} MB RSS" if rss is not None else "")
        )

        cpu = self.cpu_by_camera()
        if not cpu:
            lines.append("cpu: (first sample -- call again for a delta)")
        else:
            lines.append("cpu (% of one core, since last sample):")
            for cam_id, entry in sorted(
                cpu.items(), key=lambda kv: -kv[1]["total"]
            ):
                workers = ", ".join(
                    f"{k}={v:.1f}"
                    for k, v in sorted(entry["workers"].items(), key=lambda kv: -kv[1])
                    if v >= 0.05
                )
                lines.append(f"  {cam_id:<12} {entry['total']:6.1f}%   {workers}")

        if managers:
            mem = per_camera_memory(managers)
            if mem:
                lines.append("memory held per camera (buffers and caches only):")
                for cam_id, detail in sorted(
                    mem.items(), key=lambda kv: -kv[1]["total_mb"]
                ):
                    parts = ", ".join(
                        f"{k}={v:.1f}MB" for k, v in detail["by_source"].items() if v >= 0.05
                    )
                    lines.append(f"  {cam_id:<12} {detail['total_mb']:6.1f} MB  {parts}")

        lines.append(gpu_report())
        return "\n".join(lines)


# ===================================================================
# Memory attribution
# ===================================================================

def _array_mb(obj):
    """Megabytes held by a numpy array, or 0 for anything else."""
    nbytes = getattr(obj, "nbytes", None)
    return (nbytes / (1024 * 1024)) if isinstance(nbytes, int) else 0.0


def per_camera_memory(managers):
    """Megabytes of per-camera buffers held by each worker.

    A floor, not a total: it counts the large numpy allocations that
    dominate this app's footprint (decoded frames, zone masks, cached
    thumbnails) and ignores Python object overhead, which is noise by
    comparison. Reads attributes defensively so a worker that doesn't
    have a given buffer simply contributes nothing.
    """
    out = {}
    for label, manager in managers.items():
        try:
            cam_ids = manager.active_camera_ids()
        except AttributeError:
            continue
        for cam_id in cam_ids:
            worker = manager.get(cam_id) if hasattr(manager, "get") else None
            if worker is None:
                continue
            total = 0.0
            # Decoded frame buffer (StreamWorker).
            total += _array_mb(getattr(worker, "_latest_frame", None))
            # Cached corruption thumbnail (StreamWorker).
            total += _array_mb(getattr(worker, "_last_good_small", None))
            # Zone mask cache (MotionWorker) -- one full-frame uint8
            # mask per zone, so this grows with zone count.
            for mask in (getattr(worker, "_mask_cache", None) or {}).values():
                total += _array_mb(mask)

            entry = out.setdefault(cam_id, {"total_mb": 0.0, "by_source": {}})
            entry["total_mb"] += total
            entry["by_source"][label] = entry["by_source"].get(label, 0.0) + total
    return out


# ===================================================================
# GPU
# ===================================================================

def gpu_report():
    """Which execution provider inference is actually using, and GPU
    memory only when that provider is a GPU one."""
    try:
        import onnxruntime as ort
    except ImportError:
        return "gpu: onnxruntime not installed"

    try:
        providers = ort.get_available_providers()
    except Exception:  # noqa: BLE001 -- diagnostics must never raise
        return "gpu: could not query onnxruntime providers"

    gpu_providers = [
        p for p in providers
        if any(tag in p for tag in ("CUDA", "TensorRT", "ROCM", "DML"))
    ]
    if not gpu_providers:
        return "gpu: none -- onnxruntime is CPU-only (CPUExecutionProvider)"

    detail = _nvidia_smi()
    return f"gpu: {gpu_providers[0]} available" + (f" -- {detail}" if detail else "")


def _nvidia_smi():
    import shutil
    import subprocess

    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not out:
        return ""
    util, used, total = (x.strip() for x in out.splitlines()[0].split(","))
    return f"{util}% util, {used}/{total} MB"


# monitor = ResourceMonitor()
# print(monitor.report(managers={"stream": streams, "motion": motion, ...}))
