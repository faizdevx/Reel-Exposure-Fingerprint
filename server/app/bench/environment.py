"""What the numbers were measured on. Recorded with every result file."""

import importlib.metadata as metadata
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

PACKAGES = (
    "torch",
    "transformers",
    "onnxruntime",
    "rapidocr",
    "numpy",
    "pillow",
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "psycopg",
    "psutil",
)


def _version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _cpu_name() -> str:
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            ).stdout.strip()
            if out:
                return out.splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.processor() or "unknown"


def collect(*, cpus: int | None, threads: int) -> dict[str, Any]:
    import psutil

    memory = psutil.virtual_memory().total
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "cpu": _cpu_name(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cpus": psutil.cpu_count(logical=True),
        "memory_gb": round(memory / 2**30, 1),
        # Free memory when the run started: a run on a machine that is swapping is not a clean one.
        "available_memory_gb_at_start": round(psutil.virtual_memory().available / 2**30, 2),
        "python": platform.python_version(),
        "packages": {name: _version(name) for name in PACKAGES},
        "pinned_cpus": cpus,
        "threads": threads,
        "gpu": False,
        "env": {
            k: os.environ[k] for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS") if k in os.environ
        },
    }


def cpu_plan(count: int) -> tuple[list[int], list[int]]:
    """`count` logical CPUs on separate physical cores for the system under test, and every other
    logical CPU for the load generator, so the client does not take the server's cycles."""
    import psutil

    logical = psutil.cpu_count(logical=True) or count
    physical = psutil.cpu_count(logical=False) or logical
    step = max(1, logical // physical)
    chosen = [i * step for i in range(count)]
    if max(chosen) >= logical:
        chosen = list(range(min(count, logical)))
    rest = [i for i in range(logical) if i not in chosen]
    return chosen, rest or chosen


def pin_cpus(count: int) -> list[int]:
    """Restrict this process to `count` logical CPUs on separate physical cores where possible, to
    stand in for the spec's 4 vCPU on a bigger machine. Returns the CPUs used."""
    import psutil

    process = psutil.Process()
    logical = psutil.cpu_count(logical=True) or count
    physical = psutil.cpu_count(logical=False) or logical
    step = max(1, logical // physical)  # 2 with hyper-threading: take every other logical CPU
    chosen = [i * step for i in range(count)]
    if max(chosen) >= logical:
        chosen = list(range(min(count, logical)))
    process.cpu_affinity(chosen)
    return chosen
