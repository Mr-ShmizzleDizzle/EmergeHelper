"""Interactive prompts for the config changes a merge would need.

Nothing here writes anything on its own: each prompt returns the user's
decision and :func:`apply_changes` performs the (privileged) write in one
place, so there is exactly one path that can modify /etc/portage.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from . import scope
from .preflight import KeywordChange, LicenseChange, Preflight
from .scope import Analysis, FlagChange

KEYWORDS_FILE = "/etc/portage/package.accept_keywords/zz-emergehelper"
LICENSE_FILE = "/etc/portage/package.license/zz-emergehelper"
# Pre-rename names, still read so earlier entries aren't duplicated.
LEGACY_FILES = {
    KEYWORDS_FILE: "/etc/portage/package.accept_keywords/zz-portagehelper",
    LICENSE_FILE: "/etc/portage/package.license/zz-portagehelper",
}

SKIP = "skip"
ABORT = "abort"


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def red(self, t): return self._wrap("31", t)
    def green(self, t): return self._wrap("32", t)
    def yellow(self, t): return self._wrap("33", t)
    def blue(self, t): return self._wrap("34", t)
    def magenta(self, t): return self._wrap("35", t)
    def cyan(self, t): return self._wrap("36", t)


S = _Style(_use_color())


def _ask(prompt: str, valid: set[str], default: str) -> str:
    """Read a single choice, tolerating EOF and interrupts."""
    while True:
        try:
            raw = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return ABORT
        if not raw:
            return default
        if raw in valid:
            return raw
        print(S.dim(f"  please answer one of: {', '.join(sorted(valid))}"))


# Once a flag has been set on this many packages one at a time, keep offering
# the narrow scope but stop *recommending* it - the pattern says it's systemic.
RECURRENCE_THRESHOLD = 3


def show_use_change(
    analysis: Analysis,
    number: int,
    total: int,
    recurring: set[str] | None = None,
) -> str:
    """Present one USE change. Returns a scope name, ``skip`` or ``abort``."""
    change = analysis.change
    recurring = recurring or set()
    if len(recurring) >= RECURRENCE_THRESHOLD:
        # Move the recommendation to global scope, so the default answer
        # settles the whole class of change in one keypress.
        for option in analysis.options:
            option.recommended = option.scope == scope.SCOPE_GLOBAL
    sign = "+" if change.enable else "-"
    target = f"{change.cp}-{change.version}" if change.version else change.cp

    print()
    print(S.bold(f"USE flag change {number} of {total}") + S.dim("  ─ " + target))
    desc = analysis.description or "(no description in use.desc)"
    print(f"  {S.green(sign + change.flag)}  {S.dim(desc)}")
    if change.required_by:
        print(S.dim(f"  required by {change.required_by}"))
    print(
        S.dim(
            f"  this flag exists in {analysis.tree_matches} packages"
            f" ({analysis.installed_matches} of them installed)"
        )
    )

    if change.cycle_breaker:
        print()
        print(S.yellow("  ! this only exists to break a circular dependency"))
        print(
            S.dim(
                "    Portage can't build these in either order as configured. The\n"
                "    change is temporary - revert it once the package is installed.\n"
                "    'this version only' keeps it from lingering."
            )
        )

    previous = scope.managed_flag_state(change.cp, change.flag)
    if previous is not None and previous != change.enable:
        # Don't reverse an earlier decision without saying so.
        print()
        print(
            S.yellow(
                f"  ! eh previously set {'+' if previous else '-'}{change.flag} here; "
                f"this reverses it"
            )
        )
        print(S.dim("    Portage changed its mind while backtracking. The new line replaces the old."))

    if len(recurring) >= RECURRENCE_THRESHOLD:
        print()
        print(
            S.yellow(
                f"  ! you've already set {S.bold(change.flag)} on "
                f"{len(recurring)} packages one at a time"
            )
        )
        print(
            S.dim(
                "    Portage reveals these a few at a time, so this can repeat for"
                "\n    a while. Setting it globally settles the whole class at once."
            )
        )

    if analysis.blocked:
        print()
        print(S.yellow("  ! this flag alone won't satisfy the package's REQUIRED_USE"))
        for conflict in analysis.recommended.conflicts[:1]:
            print(S.dim(f"    {conflict.reason}"))
        if analysis.companions:
            extra = " ".join(analysis.companions)
            print(S.yellow(f"    also enabling: {S.bold(extra)} would satisfy it"))

    print()
    letters: dict[str, str] = {}
    for i, option in enumerate(analysis.options, start=1):
        key = str(i)
        letters[key] = option.scope
        tag = S.green("  [recommended]") if option.recommended else ""
        print(f"  {S.bold(key)}) {option.line:<44} {S.dim(option.label)}{tag}")
        if option.scope == scope.SCOPE_GLOBAL:
            if option.rebuilds:
                print(
                    S.dim(f"     would change {option.rebuilds} installed package(s)")
                )
            if option.conflicts:
                print(
                    S.red(f"     ! {len(option.conflicts)} would break REQUIRED_USE:")
                )
                for conflict in option.conflicts[:3]:
                    print(S.red(f"       {conflict.cp}"))
                if len(option.conflicts) > 3:
                    print(S.red(f"       …and {len(option.conflicts) - 3} more"))
            elif not option.rebuilds:
                print(S.dim("     nothing installed is affected today"))

    # Offer the companion set as its own choice when we found one.
    companion_key = ""
    if analysis.companions:
        companion_key = "c"
        combined = " ".join([change.token] + analysis.companions)
        letters["c"] = "companion"
        print(
            f"  {S.bold('c')}) {change.cp + ' ' + combined:<44} "
            f"{S.dim('this package, with the flags it requires')}"
        )

    default = str(
        next(i for i, o in enumerate(analysis.options, start=1) if o.recommended)
    )
    valid = set(letters) | {"s", "a", "d"}
    hint = f"[{default}]"
    while True:
        answer = _ask(
            f"\n  choose {S.dim('(number, ' + ('c, ' if companion_key else '') + 's=skip, d=details, a=abort)')} {hint} ",
            valid,
            default,
        )
        if answer == "d":
            _show_details(analysis)
            continue
        if answer == "s":
            return SKIP
        if answer == "a":
            return ABORT
        return letters[answer]


def _show_details(analysis: Analysis) -> None:
    change = analysis.change
    print()
    print(S.cyan("  ── details ──"))
    print(f"  flag        {change.flag}")
    print(f"  package     {change.cp}")
    if change.version:
        print(f"  version     {change.version}")
    print(f"  pulled in by{'':<1} {change.required_by or '(direct request)'}")
    print(f"  managed file{'':<1} {scope.MANAGED_FILE}")
    for option in analysis.options:
        print()
        print(f"  {S.bold(option.scope)}: writes {S.cyan(option.line)}")
        print(f"    scope      {option.label}")
        print(f"    changes    {option.rebuilds} installed package(s)")
        if option.conflicts:
            print(f"    conflicts  {len(option.conflicts)}")
            for conflict in option.conflicts[:6]:
                print(f"      {conflict.cp}: {conflict.reason}")
        else:
            print("    conflicts  none")
    print()


def show_keyword_change(change: KeywordChange, number: int, total: int) -> str:
    print()
    print(S.bold(f"Keyword change {number} of {total}") + S.dim(f"  ─ {change.atom}"))
    if change.keyword == "**":
        # No keyword for this arch at all - a much bigger claim than ~amd64,
        # which at least means "tested, not yet stable".
        print(
            f"  {S.red('**')}  "
            f"{S.dim('this package has NO keyword for your arch - it is untested here')}"
        )
        print(S.dim("     usually a live ebuild or an unkeyworded overlay package"))
    else:
        print(
            f"  {S.yellow(change.keyword)}  "
            f"{S.dim('this version is not marked stable on your arch')}"
        )
    if change.required_by:
        print(S.dim(f"  required by {change.required_by}"))
    print()
    print(f"  {S.bold('1')}) {change.atom} {change.keyword}"
          f"{S.green('   [recommended]')}")
    print(S.dim(f"     written to {KEYWORDS_FILE}"))
    answer = _ask(
        f"\n  accept? {S.dim('(1=accept, s=skip, a=abort)')} [1] ",
        {"1", "s", "a"},
        "1",
    )
    return {"1": "accept", "s": SKIP, "a": ABORT}[answer]


def show_license_change(change: LicenseChange, number: int, total: int) -> str:
    print()
    print(S.bold(f"License acceptance {number} of {total}") + S.dim(f"  ─ {change.atom}"))
    print(f"  {S.yellow(change.license)}")
    if change.required_by:
        print(S.dim(f"  required by {change.required_by}"))
    answer = _ask(
        f"\n  accept this license? {S.dim('(y=yes, s=skip, a=abort)')} [y] ",
        {"y", "s", "a"},
        "y",
    )
    return {"y": "accept", "s": SKIP, "a": ABORT}[answer]


# --- applying -------------------------------------------------------------


def _write_privileged(path: str, content: str, use_sudo: bool = True) -> bool:
    """Write ``content`` to ``path``, escalating only if we have to."""
    directory = os.path.dirname(path)
    if os.access(directory, os.W_OK) and (
        not os.path.exists(path) or os.access(path, os.W_OK)
    ):
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return True
        except OSError:
            pass
    if not use_sudo:
        return False

    # tee keeps the redirection inside the privileged process, so we never
    # need a root shell.
    try:
        proc = subprocess.run(
            ["sudo", "install", "-D", "-m", "0644", "/dev/stdin", path],
            input=content,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".emergehelper") as tmp:
            tmp.write(content)
            staged = tmp.name
        proc = subprocess.run(["sudo", "cp", staged, path], check=False)
        os.unlink(staged)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def apply_use_changes(entries: list[str], dry_run: bool = False) -> bool:
    """Merge USE entries into the eh-managed package.use file."""
    if not entries:
        return True
    superseded: list[str] = []
    merged = scope.merge_lines(scope.read_managed(), entries, removed=superseded)
    content = "\n".join(merged).rstrip() + "\n"
    if dry_run:
        print(S.dim(f"  would write {scope.MANAGED_FILE}:"))
        for entry in entries:
            print(S.dim(f"    {entry}"))
        for entry in superseded:
            print(S.dim(f"    (would drop {entry} — a wildcard now covers it)"))
        return True

    if not _write_privileged(scope.MANAGED_FILE, content):
        return False
    if superseded:
        print(S.dim("  dropped entries a wildcard now supersedes:"))
        for entry in superseded:
            print(S.dim(f"    {entry}"))
    # The legacy file's contents were folded in above; leaving it in place
    # would let its stale lines keep overriding the merged file.
    if os.path.exists(scope.LEGACY_MANAGED_FILE):
        if _remove_privileged(scope.LEGACY_MANAGED_FILE):
            print(S.dim(f"  merged and removed {scope.LEGACY_MANAGED_FILE}"))
    return True


def _remove_privileged(path: str) -> bool:
    """Delete a file we own under /etc/portage, escalating if needed."""
    try:
        os.unlink(path)
        return True
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        return subprocess.run(["sudo", "rm", "-f", path], check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _append_simple(path: str, header: str, entries: list[str], dry_run: bool) -> bool:
    if not entries:
        return True
    existing = None
    for candidate in (path, LEGACY_FILES.get(path)):
        if not candidate:
            continue
        try:
            with open(candidate, encoding="utf-8") as fh:
                existing = fh.read().splitlines()
                break
        except OSError:
            continue
    if existing is None:
        existing = [header, ""]
    known = {line.strip() for line in existing if line.strip()}
    added = [e for e in entries if e.strip() not in known]
    if not added:
        return True
    content = "\n".join(existing + added).rstrip() + "\n"
    if dry_run:
        print(S.dim(f"  would write {path}:"))
        for entry in added:
            print(S.dim(f"    {entry}"))
        return True
    return _write_privileged(path, content)


def apply_keyword_changes(entries: list[str], dry_run: bool = False) -> bool:
    return _append_simple(
        KEYWORDS_FILE,
        "# Managed by EmergeHelper (eh). Safe to edit or delete by hand.",
        entries,
        dry_run,
    )


def apply_license_changes(entries: list[str], dry_run: bool = False) -> bool:
    return _append_simple(
        LICENSE_FILE,
        "# Managed by EmergeHelper (eh). Safe to edit or delete by hand.",
        entries,
        dry_run,
    )


def resolve_interactively(
    result: Preflight,
    packages=None,
    current_use=None,
    dry_run: bool = False,
    assume_recommended: bool = False,
    written: dict[tuple[str, str], bool] | None = None,
) -> tuple[bool, int]:
    """Walk the user through every change a merge needs.

    Returns ``(proceed, applied_count)``. ``proceed`` is False when the user
    aborted; skipped changes still allow the merge to be attempted, which is
    the point of not bailing out on the first flag problem.
    """
    use_entries: list[str] = []
    keyword_entries: list[str] = []
    license_entries: list[str] = []

    written = {} if written is None else written

    total_use = len(result.use_changes)
    for i, change in enumerate(result.use_changes, start=1):
        # Portage can propose the opposite of what it asked for last round
        # while backtracking. Following that blindly flip-flops the same file
        # forever, so stop and let the user decide instead.
        prior = written.get((change.cp, change.flag))
        if prior is not None and prior != change.enable:
            print()
            print(
                S.red(
                    f"  ! Portage now wants {'+' if change.enable else '-'}{change.flag}"
                    f" on {change.cp}, the opposite of what it asked for earlier"
                )
            )
            print(
                S.dim(
                    "    This is a resolver flip-flop, not something eh can settle by\n"
                    "    writing again. Try a wider scope for this flag, or resolve\n"
                    "    the conflict by hand."
                )
            )
            return False, 0

        analysis = scope.analyze(change, packages, current_use)
        recurring = scope.managed_flag_packages(change.flag)
        if assume_recommended:
            if len(recurring) >= RECURRENCE_THRESHOLD:
                choice = scope.SCOPE_GLOBAL
            else:
                choice = analysis.recommended.scope
        else:
            choice = show_use_change(analysis, i, total_use, recurring=recurring)
        if choice == ABORT:
            return False, 0
        if choice == SKIP:
            continue
        written[(change.cp, change.flag)] = change.enable
        if choice == "companion":
            flags = " ".join([change.token] + analysis.companions)
            use_entries.append(
                f"{change.cp} {flags}"
                + (f"  # for {change.required_by}" if change.required_by else "")
            )
            continue
        option = next(o for o in analysis.options if o.scope == choice)
        use_entries.append(scope.render_addition(option, change))

    total_kw = len(result.keyword_changes)
    for i, kw in enumerate(result.keyword_changes, start=1):
        choice = "accept" if assume_recommended else show_keyword_change(kw, i, total_kw)
        if choice == ABORT:
            return False, 0
        if choice == SKIP:
            continue
        comment = f"  # for {kw.required_by}" if kw.required_by else ""
        keyword_entries.append(f"{kw.atom} {kw.keyword}{comment}")

    total_lic = len(result.license_changes)
    for i, lic in enumerate(result.license_changes, start=1):
        choice = "accept" if assume_recommended else show_license_change(lic, i, total_lic)
        if choice == ABORT:
            return False, 0
        if choice == SKIP:
            continue
        comment = f"  # for {lic.required_by}" if lic.required_by else ""
        license_entries.append(f"{lic.atom} {lic.license}{comment}")

    ok = True
    if use_entries:
        ok &= apply_use_changes(use_entries, dry_run)
    if keyword_entries:
        ok &= apply_keyword_changes(keyword_entries, dry_run)
    if license_entries:
        ok &= apply_license_changes(license_entries, dry_run)

    applied = len(use_entries) + len(keyword_entries) + len(license_entries)
    if not ok:
        print(S.red("  ! failed to write configuration changes"))
        return False, applied
    if applied and not dry_run:
        print(S.green(f"\n  wrote {applied} change(s) to /etc/portage"))
    return True, applied
