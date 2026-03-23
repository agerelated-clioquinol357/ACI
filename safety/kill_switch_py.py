"""OpenClaw Emergency Kill Switch (Python implementation).

Monitors for a global hotkey (Ctrl+Alt+Shift+Q) and terminates all
OpenClaw-related processes when triggered.  Designed to run as a
background thread alongside the main daemon, or as a standalone script.

Usage as standalone::

    python kill_switch_py.py

Usage from code::

    from safety.kill_switch_py import KillSwitch
    ks = KillSwitch()
    ks.start()        # non-blocking, runs in background thread
    # ... later ...
    ks.stop()

Requires ``psutil`` for process enumeration. The ``keyboard`` library is
optional -- if unavailable on the current platform, the kill switch falls
back to a simple polling loop that checks for a sentinel file.
"""

import os
import sys
import signal
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Process name fragments that identify OpenClaw workers
_TARGET_FRAGMENTS = ("openclaw", "bridge", "openclaw-aci")

# Sentinel file path used as a fallback trigger mechanism
_SENTINEL_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMPDIR", "/tmp")),
    "openclaw_kill_signal",
)

# Daemon notification endpoint
_DEFAULT_SHUTDOWN_URL = "http://127.0.0.1:9120/shutdown"


def _find_openclaw_processes() -> list:
    """Find all Python processes whose command line contains OpenClaw identifiers.

    Returns:
        List of ``psutil.Process`` objects (excluding the current process).
    """
    try:
        import psutil
    except ImportError:
        logger.error("psutil is required for kill switch process enumeration")
        return []

    current_pid = os.getpid()
    targets = []

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.info["pid"] == current_pid:
            continue
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            proc_name = (proc.info.get("name") or "").lower()
            combined = cmdline + " " + proc_name

            if any(frag in combined for frag in _TARGET_FRAGMENTS):
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return targets


def _kill_processes(processes: list) -> int:
    """Terminate a list of processes forcefully.

    Args:
        processes: List of ``psutil.Process`` objects.

    Returns:
        Number of processes successfully killed.
    """
    import psutil

    killed = 0
    for proc in processes:
        try:
            pid = proc.pid
            name = proc.name()
            logger.warning(f"Killing OpenClaw process: PID={pid} name={name}")

            if sys.platform == "win32":
                proc.kill()  # TerminateProcess on Windows
            else:
                os.kill(pid, signal.SIGKILL)

            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError) as e:
            logger.error(f"Failed to kill PID {proc.pid}: {e}")

    return killed


def _notify_daemon(url: str) -> bool:
    """Send a shutdown notification to the OpenClaw daemon.

    Args:
        url: HTTP endpoint to POST the shutdown signal to.

    Returns:
        True if the notification was accepted, False otherwise.
    """
    try:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            data=b'{"reason": "kill_switch_activated"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status < 400
    except Exception as e:
        logger.debug(f"Daemon notification failed (may already be down): {e}")
        return False


def emergency_stop(notify_url: Optional[str] = _DEFAULT_SHUTDOWN_URL) -> int:
    """Execute an emergency stop: kill all OpenClaw processes and notify daemon.

    This is the core function invoked by the hotkey handler.

    Args:
        notify_url: Optional URL to POST a shutdown signal to.
            Set to None to skip daemon notification.

    Returns:
        Number of processes killed.
    """
    logger.critical("EMERGENCY STOP ACTIVATED")
    print("\n!!! OPENCLAW EMERGENCY STOP !!!\n", file=sys.stderr)

    # Find and kill
    targets = _find_openclaw_processes()
    if not targets:
        logger.info("No OpenClaw processes found to kill")
        print("No OpenClaw processes found.", file=sys.stderr)
        return 0

    logger.warning(f"Found {len(targets)} OpenClaw process(es) to terminate")
    killed = _kill_processes(targets)
    logger.warning(f"Killed {killed}/{len(targets)} processes")
    print(f"Killed {killed}/{len(targets)} OpenClaw processes.", file=sys.stderr)

    # Notify daemon
    if notify_url:
        _notify_daemon(notify_url)

    return killed


class KillSwitch:
    """Background kill switch that monitors for the emergency hotkey.

    Uses the ``keyboard`` library for global hotkey detection when available.
    Falls back to a sentinel-file polling mechanism otherwise.

    Args:
        hotkey: Key combination string for the ``keyboard`` library.
            Default: ``"ctrl+alt+shift+q"``.
        notify_url: Daemon shutdown endpoint URL, or None to disable.
        poll_interval: Seconds between sentinel file checks (fallback mode).
    """

    def __init__(
        self,
        hotkey: str = "ctrl+alt+shift+q",
        notify_url: Optional[str] = _DEFAULT_SHUTDOWN_URL,
        poll_interval: float = 0.5,
    ):
        self._hotkey = hotkey
        self._notify_url = notify_url
        self._poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._mode: Optional[str] = None  # "keyboard" or "sentinel"

    def _on_hotkey(self) -> None:
        """Callback invoked when the hotkey is pressed."""
        emergency_stop(self._notify_url)

    def _run_keyboard_mode(self) -> None:
        """Monitor via the ``keyboard`` library (requires root on Linux)."""
        import keyboard as kb

        logger.info(f"Kill switch active (keyboard mode): {self._hotkey}")
        kb.add_hotkey(self._hotkey, self._on_hotkey)

        # Block until stop is requested
        self._stop_event.wait()

        try:
            kb.remove_hotkey(self._hotkey)
        except Exception:
            pass

    def _run_sentinel_mode(self) -> None:
        """Fallback: poll for a sentinel file to trigger emergency stop."""
        logger.info(f"Kill switch active (sentinel mode): watching {_SENTINEL_FILE}")
        print(
            f"Kill switch fallback mode: create '{_SENTINEL_FILE}' to trigger emergency stop.",
            file=sys.stderr,
        )

        while not self._stop_event.is_set():
            if os.path.exists(_SENTINEL_FILE):
                try:
                    os.unlink(_SENTINEL_FILE)
                except OSError:
                    pass
                emergency_stop(self._notify_url)
            self._stop_event.wait(self._poll_interval)

    def _run(self) -> None:
        """Main thread entry point -- select mode and run."""
        try:
            import keyboard  # noqa: F401
            self._mode = "keyboard"
            self._run_keyboard_mode()
        except ImportError:
            logger.info("keyboard library not available, using sentinel file fallback")
            self._mode = "sentinel"
            self._run_sentinel_mode()
        except Exception as e:
            logger.warning(f"keyboard mode failed ({e}), falling back to sentinel mode")
            self._mode = "sentinel"
            self._run_sentinel_mode()

    def start(self) -> None:
        """Start the kill switch in a background daemon thread."""
        if self._running:
            logger.warning("Kill switch already running")
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name="openclaw-kill-switch",
            daemon=True,
        )
        self._thread.start()
        logger.info("Kill switch thread started")

    def stop(self) -> None:
        """Stop the kill switch background thread."""
        if not self._running:
            return

        self._stop_event.set()
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        self._thread = None
        logger.info("Kill switch stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def mode(self) -> Optional[str]:
        return self._mode

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"<KillSwitch {state} mode={self._mode}>"


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("OpenClaw Kill Switch")
    print("====================")
    print("Press Ctrl+Alt+Shift+Q to trigger emergency stop.")
    print("Press Ctrl+C to exit the kill switch itself.")
    print()

    ks = KillSwitch()
    ks.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nKill switch shutting down.")
        ks.stop()
