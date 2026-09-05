"""Curses front-end for a running emerge.

Two views over the same :class:`~emergehelper.monitor.Monitor`:

* **compact** - a progress meter with the metrics that actually matter while
  Portage is working (phase, throughput, memory, ETA).
* **raw** - the unfiltered build output, scrollable, for when you want to watch
  the compiler.

``Ctrl+O`` switches between them, mirroring the expand-output shortcut the
terminal UI this was modelled on uses.
"""

from __future__ import annotations

import curses
import time

from .monitor import PHASE_ORDER, Monitor, State, system_metrics

CTRL_O = 15
CTRL_L = 12

VIEW_COMPACT = "compact"
VIEW_RAW = "raw"

# Colour pair ids.
C_TITLE, C_OK, C_WARN, C_ERR, C_DIM, C_ACCENT, C_BAR = range(1, 8)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_bytes(n: float) -> str:
    for unit, div in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}B"


class Screen:
    """Thin wrapper that makes curses writes bounds-safe."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.height, self.width = stdscr.getmaxyx()

    def refresh_size(self) -> None:
        self.height, self.width = self.stdscr.getmaxyx()

    def write(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Draw text, clipped to the window.

        curses raises when writing to the very last cell of the screen, so the
        final column is always left alone.
        """
        if y < 0 or y >= self.height or x >= self.width:
            return
        space = self.width - x - 1
        if space <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, space, attr)
        except curses.error:
            pass

    def hline(self, y: int, char: str = "─", attr: int = 0) -> None:
        self.write(y, 0, char * max(0, self.width - 1), attr)


class BuildUI:
    """Renders monitor state and routes keystrokes."""

    def __init__(self, monitor: Monitor, title: str = ""):
        self.monitor = monitor
        self.title = title
        self.view = VIEW_COMPACT
        self.scroll = 0  # lines from the bottom in raw view; 0 = follow tail
        self.quit_requested = False
        self._status = ""
        self._status_until = 0.0

    # --- helpers -----------------------------------------------------------
    def _flash(self, message: str, seconds: float = 2.5) -> None:
        self._status = message
        self._status_until = time.monotonic() + seconds

    def _active_status(self) -> str:
        return self._status if time.monotonic() < self._status_until else ""

    @staticmethod
    def _init_colors() -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        bg = -1
        curses.init_pair(C_TITLE, curses.COLOR_CYAN, bg)
        curses.init_pair(C_OK, curses.COLOR_GREEN, bg)
        curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
        curses.init_pair(C_ERR, curses.COLOR_RED, bg)
        curses.init_pair(C_DIM, curses.COLOR_BLUE, bg)
        curses.init_pair(C_ACCENT, curses.COLOR_MAGENTA, bg)
        curses.init_pair(C_BAR, curses.COLOR_GREEN, bg)

    @staticmethod
    def _c(pair: int, bold: bool = False) -> int:
        attr = curses.color_pair(pair) if curses.has_colors() else 0
        return attr | (curses.A_BOLD if bold else 0)

    # --- main loop ---------------------------------------------------------
    def run(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)
        self._init_colors()
        screen = Screen(stdscr)

        state = self.monitor.state
        while True:
            screen.refresh_size()
            stdscr.erase()
            if self.view == VIEW_COMPACT:
                self._draw_compact(screen, state)
            else:
                self._draw_raw(screen, state)
            stdscr.noutrefresh()
            curses.doupdate()

            key = stdscr.getch()
            if key != -1 and self._handle_key(key, screen, state):
                break

            # Build finished and the user isn't reading scrollback: fall out
            # so the CLI can print its summary.
            if not state.running and self.view == VIEW_COMPACT:
                if state.finished_at and time.monotonic() - state.finished_at > 1.2:
                    break

        return state.returncode if state.returncode is not None else 0

    def _handle_key(self, key: int, screen: Screen, state: State) -> bool:
        """Returns True when the UI should exit."""
        if key == CTRL_O:
            self.view = VIEW_RAW if self.view == VIEW_COMPACT else VIEW_COMPACT
            self.scroll = 0
            self._flash(
                "raw build output - Ctrl+O for the meter"
                if self.view == VIEW_RAW
                else "progress meter - Ctrl+O for raw output"
            )
            return False

        if key == CTRL_L:
            curses.doupdate()
            return False

        if key == curses.KEY_RESIZE:
            screen.refresh_size()
            self.monitor.resize(screen.width, screen.height)
            return False

        if key in (ord("q"), ord("Q")):
            if not state.running:
                return True
            self._flash("build still running - press Ctrl+C to cancel it")
            return False

        if key == 3:  # Ctrl+C
            self.monitor.stop()
            self._flash("sent interrupt to emerge")
            return False

        # Scrolling only means something over the raw log.
        if self.view == VIEW_RAW:
            page = max(1, screen.height - 4)
            with state.lock:
                total = len(state.lines)
            visible = max(1, screen.height - 3)
            max_scroll = max(0, total - visible)
            if key in (curses.KEY_UP, ord("k")):
                self.scroll = min(max_scroll, self.scroll + 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.scroll = max(0, self.scroll - 1)
            elif key == curses.KEY_PPAGE:
                self.scroll = min(max_scroll, self.scroll + page)
            elif key == curses.KEY_NPAGE:
                self.scroll = max(0, self.scroll - page)
            elif key == curses.KEY_HOME:
                self.scroll = max_scroll
            elif key == curses.KEY_END:
                self.scroll = 0
        return False

    # --- compact view ------------------------------------------------------
    def _draw_compact(self, s: Screen, state: State) -> None:
        with state.lock:
            cpv = state.current_cpv
            phase = state.current_phase
            idx, total = state.package_index, state.package_total
            jobs_done, jobs_total, jobs_running, jobs_failed = (
                state.jobs_done,
                state.jobs_total,
                state.jobs_running,
                state.jobs_failed,
            )
            binary = state.is_binary
            completed = list(state.completed)
            failed = list(state.failed)
            elapsed = state.elapsed
            pkg_elapsed = state.package_elapsed
            fraction = state.progress_fraction()
            eta = state.eta_seconds()
            avg_cpu = state.average_cpu(30)
            avg_rss = state.average_rss(30)
            peak_rss = state.peak_rss()
            samples = len(state.samples)
            running = state.running
            rc = state.returncode
            tail = list(state.lines)[-6:]

        row = 0
        header = self.title or "EmergeHelper"
        s.write(row, 0, f" {header} ", self._c(C_TITLE, True))
        right = "running" if running else (f"done (exit {rc})" if rc else "done")
        s.write(row, max(0, s.width - len(right) - 2), right,
                self._c(C_OK if running or not rc else C_ERR, True))
        row += 1
        s.hline(row, "─", self._c(C_DIM))
        row += 1

        # Overall progress bar.
        bar_width = max(10, s.width - 32)
        filled = int(bar_width * fraction)
        s.write(row, 0, "Overall ", self._c(C_DIM))
        s.write(row, 8, "█" * filled, self._c(C_BAR, True))
        s.write(row, 8 + filled, "░" * (bar_width - filled), self._c(C_DIM))
        pct = f" {fraction * 100:5.1f}%"
        s.write(row, 8 + bar_width, pct, self._c(C_OK, True))
        row += 1

        counter = (
            f"{jobs_done}/{jobs_total} jobs"
            if jobs_total
            else (f"package {idx}/{total}" if total else "resolving")
        )
        s.write(row, 8, f"{counter}   elapsed {_fmt_duration(elapsed)}   eta {_fmt_duration(eta)}",
                self._c(C_DIM))
        extra_x = 8 + 52
        if jobs_running:
            token = f"{jobs_running} running"
            s.write(row, extra_x, token, self._c(C_ACCENT))
            extra_x += len(token) + 2
        if jobs_failed:
            # --keep-going means a failure doesn't stop the run, so this has
            # to be visible without switching to the raw log.
            s.write(row, extra_x, f"{jobs_failed} failed", self._c(C_ERR, True))
        row += 2

        # Current package and its phase pipeline.
        if cpv:
            label = "Building" if not binary else "Unpacking binary"
            s.write(row, 0, f"{label} ", self._c(C_DIM))
            s.write(row, len(label) + 1, cpv, self._c(C_ACCENT, True))
            s.write(row, len(label) + 2 + len(cpv), f"  {_fmt_duration(pkg_elapsed)}",
                    self._c(C_DIM))
            row += 1
            row = self._draw_phases(s, row, phase)
        else:
            s.write(row, 0, "Waiting for Portage to start the first job…", self._c(C_DIM))
            row += 1
        row += 1

        # Resource metrics.
        sysm = system_metrics()
        cores = int(sysm.get("cpu_count") or 1)
        cpu_norm = (avg_cpu / (cores * 100)) * 100 if cores else 0
        s.write(row, 0, "Resources", self._c(C_TITLE, True))
        row += 1
        metrics = [
            ("build cpu", f"{avg_cpu:6.0f}%  ({cpu_norm:4.1f}% of {cores} cores)"),
            ("build ram", f"{_fmt_bytes(avg_rss):>7}  peak {_fmt_bytes(peak_rss)}"),
            ("load avg", f"{sysm.get('load1', 0):6.2f}  {sysm.get('load5', 0):.2f}"
                         f"  {sysm.get('load15', 0):.2f}"),
            ("sys ram", f"{_fmt_bytes(sysm.get('mem_used', 0)):>7} / "
                        f"{_fmt_bytes(sysm.get('mem_total', 0))}"
                        f"  ({sysm.get('mem_percent', 0):.0f}%)"),
            ("build tmp", f"{_fmt_bytes(sysm.get('tmp_free', 0)):>7} free"),
        ]
        for name, value in metrics:
            s.write(row, 2, f"{name:<10}", self._c(C_DIM))
            s.write(row, 13, value)
            row += 1
        if samples == 0 and running:
            s.write(row, 2, "sampling…", self._c(C_DIM))
            row += 1
        row += 1

        # Completed / failed summary.
        if completed or failed:
            s.write(row, 0, "Completed", self._c(C_TITLE, True))
            row += 1
            for cp in completed[-3:]:
                s.write(row, 2, f"✓ {cp}", self._c(C_OK))
                row += 1
            for cp in failed:
                s.write(row, 2, f"✗ {cp}", self._c(C_ERR, True))
                row += 1
            row += 1

        # A peek at the tail, so the compact view still shows life.
        peek_room = s.height - row - 2
        if peek_room > 2 and tail:
            s.write(row, 0, "Latest output", self._c(C_TITLE, True))
            row += 1
            for line in tail[-(peek_room - 1):]:
                s.write(row, 2, line, self._c(C_DIM))
                row += 1

        self._draw_footer(s, state)

    def _draw_phases(self, s: Screen, row: int, phase: str) -> int:
        """Render the build pipeline with the active stage highlighted."""
        x = 2
        for name in PHASE_ORDER:
            if name == phase:
                attr, marker = self._c(C_OK, True), "●"
            elif PHASE_ORDER.index(name) < (
                PHASE_ORDER.index(phase) if phase in PHASE_ORDER else -1
            ):
                attr, marker = self._c(C_DIM), "✓"
            else:
                attr, marker = self._c(C_DIM), "○"
            token = f"{marker} {name}"
            if x + len(token) + 3 >= s.width:
                break
            s.write(row, x, token, attr)
            x += len(token)
            if name != PHASE_ORDER[-1]:
                s.write(row, x, " → ", self._c(C_DIM))
                x += 3
        return row + 1

    # --- raw view ----------------------------------------------------------
    def _draw_raw(self, s: Screen, state: State) -> None:
        with state.lock:
            lines = list(state.lines)
            running = state.running

        s.write(0, 0, " Build output ", self._c(C_TITLE, True))
        position = "following" if self.scroll == 0 else f"scrolled +{self.scroll}"
        s.write(0, max(0, s.width - len(position) - 2), position, self._c(C_ACCENT))
        s.hline(1, "─", self._c(C_DIM))

        visible = max(1, s.height - 3)
        end = len(lines) - self.scroll
        start = max(0, end - visible)
        window = lines[start:end]

        for i, line in enumerate(window):
            attr = 0
            stripped = line.strip()
            if stripped.startswith(">>>"):
                attr = self._c(C_OK, True)
            elif stripped.startswith("!!!") or "error:" in stripped.lower():
                attr = self._c(C_ERR, True)
            elif stripped.startswith("*") or "warning:" in stripped.lower():
                attr = self._c(C_WARN)
            s.write(2 + i, 0, line, attr)

        if not lines:
            s.write(2, 0, "(no output yet)", self._c(C_DIM))

        self._draw_footer(s, state, raw=True)

    # --- footer ------------------------------------------------------------
    def _draw_footer(self, s: Screen, state: State, raw: bool = False) -> None:
        y = s.height - 1
        status = self._active_status()
        if status:
            s.write(y, 0, f" {status} ", self._c(C_WARN, True))
            return
        if raw:
            keys = "Ctrl+O meter   ↑↓/PgUp/PgDn scroll   End follow   Ctrl+C cancel"
        else:
            keys = "Ctrl+O raw output   Ctrl+C cancel" + (
                "   q quit" if not state.running else ""
            )
        s.write(y, 0, f" {keys}", self._c(C_DIM))


def run_ui(monitor: Monitor, title: str = "") -> int:
    """Drive the UI, guaranteeing the terminal is restored on any exit."""
    ui = BuildUI(monitor, title=title)
    return curses.wrapper(ui.run)
