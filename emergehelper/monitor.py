"""Run emerge under a PTY, track what it's doing, and sample what it costs.

emerge is driven through a pseudo-terminal rather than a pipe so it behaves
exactly as it would interactively (progress bars, colour, unbuffered writes).
We tee every byte to a log file, keep a bounded tail in memory for the
expanded view, and parse the ``>>>`` markers Portage emits to derive the
current build phase.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import tty
import time
from collections import deque
from dataclasses import dataclass, field

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is present on this system
    psutil = None

# ">>> Emerging (1 of 5) app-admin/conky-1.22.3::gentoo".
# Portage appends " binary" for binhost packages (MergeListItem.py) and uses
# "Fetching" instead when running --fetchonly.
_EMERGING = re.compile(
    r">>>\s+(?P<action>Emerging|Fetching)\s+(?P<binary>binary\s+)?"
    r"\((?P<n>\d+)\s+of\s+(?P<total>\d+)\)\s+(?P<cpv>\S+)"
)
# ">>> Installing (1 of 5) app-admin/conky-1.22.3::gentoo"
_INSTALLING = re.compile(
    r">>>\s+Installing\s+\((?P<n>\d+)\s+of\s+(?P<total>\d+)\)\s+(?P<cpv>\S+)"
)
# "Jobs: 2 of 15 complete, 3 running, 1 failed" (JobStatusDisplay.py).
# The ">>>" prefix is optional: this also appears on the status line.
_JOBS = re.compile(
    r"Jobs:\s+(?P<done>\d+)\s+of\s+(?P<total>\d+)\s+complete"
    r"(?:,\s+(?P<running>\d+)\s+running)?"
    r"(?:,\s+(?P<failed>\d+)\s+failed)?"
)
_FAILED = re.compile(r">>>\s+Failed to (?:emerge|build|install)\s+(?P<cpv>\S+)")

# Verified against Portage's own emitters: the phase messages come from
# ebuild.sh / phase-functions.sh, the rest from _emerge.
_PHASE_MARKERS: tuple[tuple[str, str], ...] = (
    ("Downloading", "fetch"),
    ("Fetching", "fetch"),
    ("Unpacking source", "unpack"),
    ("Unpacking", "unpack"),
    ("Source unpacked", "unpack"),
    ("Preparing source", "prepare"),
    ("Source prepared", "prepare"),
    ("Configuring source", "configure"),
    ("Source configured", "configure"),
    ("Compiling source", "compile"),
    ("Source compiled", "compile"),
    ("Completed testing", "test"),
    ("Test phase", "test"),
    ("Completed installing", "install"),
    ("Install ", "install"),
    ("Merging", "merge"),
    ("Recording", "merge"),
)

PHASE_ORDER = ("fetch", "unpack", "prepare", "configure", "compile", "test", "install", "merge")

# Strip ANSI escapes when matching, so colourised output still parses.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r")


# Output that is unmistakably Portage's, marking the end of any sudo exchange.
_PORTAGE_MARKERS = (
    ">>>",
    "Calculating dependencies",
    "These are the packages",
    "emerge:",
    "Local copy of remote index",
    "!!!",
    "[ebuild",
    "[binary",
)


def _first_portage_marker(text: str) -> int | None:
    """Index of the earliest Portage marker in ``text``, or None."""
    found = [text.find(m) for m in _PORTAGE_MARKERS]
    hits = [i for i in found if i >= 0]
    return min(hits) if hits else None


@dataclass(slots=True)
class Sample:
    """One resource reading of the build process tree."""

    at: float
    cpu_percent: float
    rss_bytes: int
    procs: int


@dataclass
class State:
    """Everything the UI renders. Guarded by :attr:`lock`."""

    lock: threading.RLock = field(default_factory=threading.RLock)

    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    returncode: int | None = None

    current_cpv: str = ""
    current_phase: str = ""
    package_index: int = 0
    package_total: int = 0
    is_binary: bool = False
    package_started_at: float = 0.0

    jobs_done: int = 0
    jobs_total: int = 0
    jobs_running: int = 0
    jobs_failed: int = 0

    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    samples: deque[Sample] = field(default_factory=lambda: deque(maxlen=600))
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    log_path: str = ""

    # --- derived views -----------------------------------------------------
    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    @property
    def package_elapsed(self) -> float:
        if not self.package_started_at:
            return 0.0
        return time.monotonic() - self.package_started_at

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def recent(self, seconds: float = 30.0) -> list[Sample]:
        cutoff = time.monotonic() - seconds
        return [s for s in self.samples if s.at >= cutoff]

    def average_cpu(self, seconds: float = 30.0) -> float:
        window = self.recent(seconds)
        return sum(s.cpu_percent for s in window) / len(window) if window else 0.0

    def average_rss(self, seconds: float = 30.0) -> float:
        window = self.recent(seconds)
        return sum(s.rss_bytes for s in window) / len(window) if window else 0.0

    def peak_rss(self) -> int:
        return max((s.rss_bytes for s in self.samples), default=0)

    def progress_fraction(self) -> float:
        """Overall completion in 0..1, preferring emerge's own job counter."""
        if self.jobs_total:
            return min(1.0, self.jobs_done / self.jobs_total)
        if self.package_total:
            return min(1.0, max(0, self.package_index - 1) / self.package_total)
        return 0.0

    def eta_seconds(self) -> float | None:
        """Linear ETA from observed throughput. None until there's a basis."""
        done = self.jobs_done or max(0, self.package_index - 1)
        total = self.jobs_total or self.package_total
        if not total or done <= 0:
            return None
        rate = self.elapsed / done
        remaining = total - done
        return rate * remaining if remaining > 0 else 0.0


class Monitor:
    """Owns the emerge subprocess, its PTY, and the sampling threads."""

    def __init__(self, cmd: list[str], log_path: str, env: dict[str, str] | None = None):
        self.cmd = cmd
        self.state = State(log_path=log_path)
        self.env = env or dict(os.environ)
        self.proc: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._log = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._pending = ""  # partial line carried between reads

    # --- lifecycle ---------------------------------------------------------
    def start(self, cols: int = 120, rows: int = 40, defer_reader: bool = False) -> None:
        master, slave = pty.openpty()
        self._set_winsize(master, rows, cols)

        # Portage adapts to COLUMNS; keep it consistent with the PTY size.
        env = dict(self.env)
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)

        try:
            os.makedirs(os.path.dirname(self.state.log_path), exist_ok=True)
            self._log = open(self.state.log_path, "wb", buffering=0)
        except OSError:
            self._log = None

        def _become_session_leader() -> None:
            """Give the child its own session *with* the PTY as its terminal.

            setsid() alone (start_new_session=True) leaves the child with no
            controlling terminal, and sudo refuses to prompt without one
            ("a terminal is required to read the password"). TIOCSCTTY on the
            slave — which is fd 0 by the time preexec_fn runs — attaches it, so
            we get both a signalable process group and a usable terminal.
            """
            os.setsid()
            try:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            except OSError:
                pass

        self.proc = subprocess.Popen(
            self.cmd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            close_fds=True,
            preexec_fn=_become_session_leader,
        )
        os.close(slave)
        self._master_fd = master

        targets = [self._sampler, self._waiter]
        # The reader can be held back so a caller may bridge the real terminal
        # to the PTY first (a sudo password prompt needs that); two consumers
        # on the same fd would race for bytes.
        if not defer_reader:
            targets.insert(0, self._reader)
        for target in targets:
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def start_reader(self) -> None:
        """Begin draining the PTY, after any interactive prelude has finished."""
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()
        self._threads.append(t)

    def portage_started(self) -> bool:
        """True once output that is clearly Portage's has appeared.

        Used to decide when an interactive prelude (typically sudo asking for
        a password) is over and the full-screen UI can take the terminal.
        """
        markers = (
            ">>>",
            "Calculating dependencies",
            "These are the packages",
            "emerge:",
            "Local copy of remote index",
            "!!!",
        )
        with self.state.lock:
            for line in self.state.lines:
                if any(m in line for m in markers):
                    return True
        return False

    def bridge(self, timeout: float = 300.0, poll: float = 0.05) -> None:
        """Wire the user's terminal to the child until Portage gets going.

        emerge is launched through sudo on a PTY of our own, and sudo binds its
        authentication timestamp to the tty it was asked on. Priming with
        ``sudo -v`` on the real terminal therefore doesn't count for this one,
        and the password prompt has to be answerable here. Everything read is
        still fed to the parser and the log, so nothing is lost when the UI
        takes over.
        """
        if self._master_fd is None or self.proc is None:
            return
        try:
            stdin_fd = sys.stdin.fileno()
            interactive = os.isatty(stdin_fd)
        except (ValueError, OSError):
            interactive = False

        if not interactive:
            # Nothing to forward from, so bridging would only stall. Hand
            # straight over to the reader.
            self.start_reader()
            return

        saved = None
        if interactive:
            try:
                saved = termios.tcgetattr(stdin_fd)
                tty.setraw(stdin_fd)
            except termios.error:
                saved = None

        # NOTHING read during this phase is logged or kept in state until we
        # know the credential exchange is over. A PTY can echo what is typed
        # at a password prompt, so logging here would write the user's sudo
        # password to disk in plaintext.
        buffered = ""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    break
                watch = [self._master_fd]
                if interactive:
                    watch.append(stdin_fd)
                try:
                    readable, _, _ = select.select(watch, [], [], poll)
                except (OSError, ValueError):
                    break

                if self._master_fd in readable:
                    try:
                        chunk = os.read(self._master_fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    # Echo to the real terminal so the prompt is visible; the
                    # terminal is where a password belongs, unlike our log.
                    try:
                        os.write(1, chunk)
                    except OSError:
                        pass
                    buffered += chunk.decode("utf-8", "replace")

                if interactive and stdin_fd in readable:
                    try:
                        typed = os.read(stdin_fd, 4096)
                    except OSError:
                        typed = b""
                    if typed:
                        try:
                            os.write(self._master_fd, typed)
                        except OSError:
                            pass

                if _first_portage_marker(buffered) is not None:
                    break
        finally:
            if saved is not None:
                try:
                    termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
                except termios.error:
                    pass

        self._absorb_prelude(buffered)
        self.start_reader()

    def _absorb_prelude(self, buffered: str) -> None:
        """Keep the Portage part of the prelude; discard the sudo exchange."""
        start = _first_portage_marker(buffered)
        if start is not None:
            keep = buffered[start:]
        else:
            # No Portage output: the run failed during authentication. Keep
            # only sudo's own diagnostics, never the echoed input.
            keep = "\n".join(
                line
                for line in buffered.splitlines()
                if line.strip().startswith(("sudo:", "Sorry, try again"))
            )
            if keep:
                keep += "\n"
        if not keep:
            return
        if self._log:
            try:
                self._log.write(keep.encode("utf-8", "replace"))
            except OSError:
                pass
        self._consume(keep)

    @staticmethod
    def _set_winsize(fd: int, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if self._master_fd is not None:
            self._set_winsize(self._master_fd, rows, cols)

    def send(self, data: bytes) -> None:
        """Forward a keystroke to emerge (used for interactive prompts)."""
        if self._master_fd is not None:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def stop(self, sig: int = signal.SIGINT) -> None:
        """Signal the whole build process group."""
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), sig)
            except (OSError, ProcessLookupError):
                try:
                    self.proc.send_signal(sig)
                except OSError:
                    pass

    def wait(self, timeout: float | None = None) -> int | None:
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self) -> None:
        self._stop.set()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._log:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    # --- threads -----------------------------------------------------------
    def _reader(self) -> None:
        """Pump the PTY: tee to the log file and feed the line parser."""
        fd = self._master_fd
        assert fd is not None
        while not self._stop.is_set():
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                # EIO is the normal signal that the child closed the PTY.
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            if not chunk:
                break
            if self._log:
                try:
                    self._log.write(chunk)
                except OSError:
                    pass
            self._consume(chunk.decode("utf-8", "replace"))
        self._flush_pending()

    def _consume(self, text: str) -> None:
        self._pending += text
        # Treat CR as a line break too, so progress-bar redraws become
        # discrete lines instead of one ever-growing line.
        normalized = self._pending.replace("\r\n", "\n").replace("\r", "\n")
        *complete, self._pending = normalized.split("\n")
        for line in complete:
            self._handle_line(line)

    def _flush_pending(self) -> None:
        if self._pending:
            self._handle_line(self._pending)
            self._pending = ""

    def _handle_line(self, raw: str) -> None:
        clean = _ANSI.sub("", raw).rstrip()
        state = self.state
        with state.lock:
            # Store the cleaned text: curses can't interpret ANSI, so raw
            # escapes (colour, and the OSC title Portage sets) would render as
            # literal garbage. The log file still gets the untouched bytes.
            if clean or not raw.strip():
                state.lines.append(clean)

            m = _EMERGING.search(clean)
            if m:
                if state.current_cpv and state.current_cpv not in state.completed:
                    state.completed.append(state.current_cpv)
                state.current_cpv = m.group("cpv").split("::")[0]
                state.package_index = int(m.group("n"))
                state.package_total = int(m.group("total"))
                state.is_binary = bool(m.group("binary"))
                state.package_started_at = time.monotonic()
                state.current_phase = "fetch" if not state.is_binary else "merge"
                return

            m = _INSTALLING.search(clean)
            if m:
                state.current_phase = "merge"
                return

            m = _JOBS.search(clean)
            if m:
                state.jobs_done = int(m.group("done"))
                state.jobs_total = int(m.group("total"))
                state.jobs_running = int(m.group("running") or 0)
                state.jobs_failed = int(m.group("failed") or 0)
                return

            m = _FAILED.search(clean)
            if m:
                cpv = m.group("cpv").split("::")[0]
                if cpv not in state.failed:
                    state.failed.append(cpv)
                return

            if clean.startswith(">>>"):
                body = clean[3:].strip()
                for marker, phase in _PHASE_MARKERS:
                    if body.startswith(marker):
                        state.current_phase = phase
                        break

    def _sampler(self, interval: float = 1.0) -> None:
        """Sample CPU/RSS across the emerge process tree."""
        if psutil is None or self.proc is None:
            return
        try:
            root = psutil.Process(self.proc.pid)
        except Exception:
            return

        # cpu_percent() is measured between calls, so the first pass only
        # primes the counters and its reading is meaningless.
        tracked: dict[int, object] = {}
        primed = False
        while not self._stop.is_set():
            time.sleep(interval)
            cpu = 0.0
            rss = 0
            count = 0
            try:
                procs = [root] + root.children(recursive=True)
            except Exception:
                break

            alive: dict[int, object] = {}
            for p in procs:
                try:
                    prev = tracked.get(p.pid)
                    if prev is None:
                        p.cpu_percent(None)  # prime this process
                        alive[p.pid] = p
                        rss += p.memory_info().rss
                        count += 1
                        continue
                    cpu += prev.cpu_percent(None)
                    rss += prev.memory_info().rss
                    count += 1
                    alive[p.pid] = prev
                except Exception:
                    continue
            tracked = alive

            if primed:
                with self.state.lock:
                    self.state.samples.append(
                        Sample(at=time.monotonic(), cpu_percent=cpu, rss_bytes=rss, procs=count)
                    )
            primed = True
            if self.proc.poll() is not None:
                break

    def _waiter(self) -> None:
        if self.proc is None:
            return
        rc = self.proc.wait()
        with self.state.lock:
            if self.state.current_cpv and self.state.current_cpv not in self.state.completed:
                if rc == 0:
                    self.state.completed.append(self.state.current_cpv)
            self.state.returncode = rc
            self.state.finished_at = time.monotonic()
        # Let the reader drain any final buffered output.
        time.sleep(0.3)
        self._stop.set()


def system_metrics() -> dict[str, float]:
    """Whole-machine context shown alongside the build's own usage."""
    out: dict[str, float] = {}
    try:
        out["load1"], out["load5"], out["load15"] = os.getloadavg()
    except OSError:
        pass
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            out["mem_total"] = vm.total
            out["mem_used"] = vm.total - vm.available
            out["mem_percent"] = vm.percent
            out["cpu_count"] = psutil.cpu_count() or 0
        except Exception:
            pass
        for label, path in (("tmp_free", "/var/tmp"), ("pkg_free", "/var/cache")):
            try:
                usage = psutil.disk_usage(path)
                out[label] = usage.free
            except Exception:
                pass
    return out
