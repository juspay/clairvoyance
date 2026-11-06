import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_a_dead_thread_takes_its_process_down():
    # A critical thread that dies must not leave a process that looks alive
    # (to its supervisor) but does nothing.
    code = (
        "import threading, time\n"
        "from stt import log, proc\n"
        "log.setup('t', 'INFO', False)\n"
        "proc.install_crash_handlers('t')\n"
        "threading.Thread(target=lambda: 1 / 0).start()\n"
        "time.sleep(5)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True, timeout=10
    )
    assert result.returncode == 1
    assert b"died" in result.stderr  # the log line was flushed before exiting
