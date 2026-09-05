"""``eh`` - the EmergeHelper command line."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

from . import complete as completion
from . import index, monitor, preflight, prompts, scope
from .prompts import S

VERSION = "1.0.0"


def log_dir() -> str:
    return os.path.join(index.cache_dir(), "logs")


def _new_log_path() -> str:
    return os.path.join(log_dir(), time.strftime("emerge-%Y%m%d-%H%M%S.log"))


def _sudo_available() -> bool:
    """Check we can escalate at all, without trying to authenticate yet.

    Deliberately does *not* run `sudo -v` to pre-authenticate. As a nested
    subprocess sudo never renders its prompt here, yet still burns all three
    attempts — the user sees "Sorry, try again" with nothing to type into.
    Authentication happens once, in the PTY, where sudo is the direct child
    and prompts correctly.
    """
    if os.geteuid() == 0:
        return True
    if shutil.which("sudo") is None:
        print(S.red("sudo not found; run eh as root instead"))
        return False
    return True


def _sudo_already_valid() -> bool:
    """True when a sudo timestamp is live, so no prompt will be needed."""
    if os.geteuid() == 0:
        return True
    try:
        return (
            subprocess.run(
                ["sudo", "-n", "true"], capture_output=True, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _interactive_stdin() -> bool:
    try:
        return os.isatty(sys.stdin.fileno())
    except (ValueError, OSError):
        return False


def _emerge_command(atoms: list[str], opts: list[str]) -> list[str]:
    cmd: list[str] = []
    if os.geteuid() != 0:
        cmd.append("sudo")
        if not _interactive_stdin():
            # No terminal to type a password on. Without -n sudo would sit on
            # the PTY waiting for input that can never arrive.
            cmd.append("-n")
    cmd.append("emerge")
    cmd.extend(opts)
    cmd.extend(atoms)
    return cmd


# --- commands --------------------------------------------------------------


def cmd_install(args) -> int:
    atoms = args.atoms
    if not atoms:
        print(S.red("nothing to install"))
        return 2

    print(S.bold(f"Resolving {' '.join(atoms)}…"))
    result = preflight.run(
        atoms,
        emerge_opts=args.emerge_opts,
        use_binpkg=not args.no_binpkg,
    )

    hard_masked = [m for m in result.masked if m.hard_masked]
    if hard_masked and not result.needs_changes:
        print(S.red("\nThese packages are masked by a maintainer (package.mask):"))
        for entry in hard_masked[:5]:
            location = f"::{entry.repo}" if entry.repo else ""
            print(f"  {entry.cpv}{location}")
            print(S.dim(f"    {', '.join(entry.reasons)}"))
        print()
        print(
            S.dim(
                "eh won't offer to unmask these: a package.mask entry means someone\n"
                "marked the package broken, unsafe, or being removed. Check the mask\n"
                "reason first (portageq, or the repo's profiles/package.mask)."
            )
        )
        return 1

    if result.blockers and not result.needs_changes:
        print(S.red("\nPortage could not resolve this request:"))
        for blocker in result.blockers[:6]:
            print(f"  {blocker}")
        if result.violations:
            print()
            print(S.yellow("REQUIRED_USE constraints involved:"))
            for cpv, rule in result.violations[:3]:
                print(f"  {cpv}")
                print(S.dim(f"    {rule[:160]}"))
            # This is precisely the case where a bare emerge just gives up.
            _suggest_for_violations(result)
        return 1

    # Portage reports only the changes blocking the *current* resolve, so
    # satisfying one batch can reveal the next (a 32-bit multilib stack for
    # Steam surfaces a few packages at a time). Keep resolving until the plan
    # comes back clean rather than making the user re-run eh for each round.
    packages = index.load()
    current_use = index.installed_use()
    round_number = 0
    total_applied = 0
    written: dict[tuple[str, str], bool] = {}
    # Generous, because each round is one more package in a dependency chain
    # and stopping early just makes the user re-run. The recurrence hint below
    # is what actually keeps the count down.
    MAX_ROUNDS = 40

    while result.needs_changes:
        round_number += 1
        if round_number > MAX_ROUNDS:
            print(
                S.yellow(
                    f"\nStill finding new requirements after {MAX_ROUNDS} rounds; stopping."
                )
            )
            print(
                S.dim(
                    f"  {total_applied} change(s) were applied and are kept.\n"
                    "  Run eh again to carry on, or pick the global scope for the flag\n"
                    "  that keeps recurring to settle it in one go."
                )
            )
            return 1

        counts = []
        if result.use_changes:
            counts.append(f"{len(result.use_changes)} USE")
        if result.keyword_changes:
            counts.append(f"{len(result.keyword_changes)} keyword")
        if result.license_changes:
            counts.append(f"{len(result.license_changes)} license")
        suffix = f"  (round {round_number})" if round_number > 1 else ""
        print(
            S.yellow(f"\nThis merge needs {', '.join(counts)} change(s) before it can run.{suffix}")
        )
        if round_number == 1:
            print(S.dim("Nothing is written until you choose."))

        proceed, applied = prompts.resolve_interactively(
            result,
            packages=packages,
            current_use=current_use,
            dry_run=args.pretend,
            assume_recommended=args.yes,
            written=written,
        )
        total_applied += applied
        if not proceed:
            if total_applied:
                print(
                    S.dim(
                        f"\nstopped; {total_applied} earlier change(s) remain written "
                        "(see 'eh changes')"
                    )
                )
            else:
                print(S.dim("\naborted; nothing was changed"))
            return 1
        if args.pretend:
            return 0
        if not applied:
            # Everything was skipped: re-resolving would loop forever on the
            # same set, so try the merge and let emerge have the final say.
            print(S.dim("\nno changes applied; attempting the merge anyway"))
            break

        print(S.dim("\nre-checking with the new configuration…"))
        current_use = index.installed_use()
        previous_signature = _change_signature(result)
        result = preflight.run(
            atoms,
            emerge_opts=args.emerge_opts,
            use_binpkg=not args.no_binpkg,
        )
        # If Portage asks for exactly what we just wrote, the write didn't
        # take effect. Repeating the prompt would loop forever, so stop and
        # say what to look at.
        if result.needs_changes and _change_signature(result) == previous_signature:
            print()
            print(S.red("The same change is being requested again after writing it."))
            print(
                S.dim(
                    "  That means something else in /etc/portage is overriding it.\n"
                    "  Portage resolves package.use by atom specificity, not file order,\n"
                    "  so a per-package line always beats a */* wildcard no matter where\n"
                    "  it appears. Check for a conflicting entry:\n"
                )
            )
            for change in result.use_changes[:4]:
                print(S.dim(f"    grep -rn '{change.flag}' /etc/portage/package.use/"))
            print(S.dim(f"\n  {total_applied} change(s) applied so far are kept."))
            return 1
        if result.blockers and not result.resolved and not result.needs_changes:
            print(S.red("still unresolved:"))
            for blocker in result.blockers[:4]:
                print(f"  {blocker}")
            return 1

    if result.packages:
        print(S.dim(f"\n{len(result.packages)} package(s) to merge:"))
        for cpv in result.packages[:12]:
            print(S.dim(f"  {cpv}"))
        if len(result.packages) > 12:
            print(S.dim(f"  …and {len(result.packages) - 12} more"))

    if args.pretend:
        return 0

    if not _sudo_available():
        return 1

    return _run_with_ui(atoms, args)


def _change_signature(result: preflight.Preflight) -> tuple:
    """A comparable fingerprint of what a resolve is asking for."""
    return (
        tuple(sorted((c.cp, c.flag, c.enable) for c in result.use_changes)),
        tuple(sorted((k.atom, k.keyword) for k in result.keyword_changes)),
        tuple(sorted((l.atom, l.license) for l in result.license_changes)),
    )


def _suggest_for_violations(result: preflight.Preflight) -> None:
    """Turn a REQUIRED_USE refusal into an actionable suggestion."""
    packages = index.load()
    by_cp = {p.cp: p for p in packages}
    current_use = index.installed_use()
    for cpv, _rule in result.violations[:3]:
        cp, version = preflight.split_cpv(cpv.split("::")[0])
        pkg = by_cp.get(cp)
        if pkg is None:
            continue
        for change in result.use_changes:
            if change.cp != cp:
                continue
            companions = scope.suggest_companions(pkg, change, current_use)
            if companions:
                flags = " ".join([change.token] + companions)
                print()
                print(S.green("  eh suggests:"))
                print(f"    {cp} {flags}")
                print(S.dim(f"    add that to {scope.MANAGED_FILE} and retry"))
            break


def _run_with_ui(atoms: list[str], args) -> int:
    opts = list(args.emerge_opts)
    # The UI parses these markers, and --quiet-build would suppress them.
    if not any(o.startswith("--jobs") for o in opts):
        pass  # respect EMERGE_DEFAULT_OPTS
    cmd = _emerge_command(atoms, opts)
    log_path = _new_log_path()

    if args.plain:
        print(S.dim(f"running: {' '.join(cmd)}"))
        return subprocess.run(cmd, check=False).returncode

    try:
        size = os.get_terminal_size()
        cols, rows = size.columns, size.lines
    except OSError:
        cols, rows = 120, 40

    mon = monitor.Monitor(cmd, log_path)
    # Hold the reader back and pass the terminal through first: sudo ties its
    # auth timestamp to the tty it was asked on, so priming with `sudo -v`
    # doesn't cover the PTY we just made and the password has to be typed here.
    # Bridge the real terminal to the PTY whenever sudo might still ask for a
    # password, so the prompt is answerable before curses takes the screen.
    needs_password = (
        os.geteuid() != 0 and _interactive_stdin() and not _sudo_already_valid()
    )
    if needs_password:
        print(S.dim("emerge needs root — sudo will ask for your password below."))
    mon.start(cols=cols, rows=rows, defer_reader=needs_password)
    if needs_password:
        mon.bridge()

    from .ui import run_ui

    title = f"EmergeHelper — emerge {' '.join(atoms)}"
    try:
        run_ui(mon, title=title)
    except Exception as exc:  # keep the terminal usable no matter what
        print(S.red(f"UI error: {exc}"))
    rc = mon.wait(timeout=5)
    mon.close()
    if rc is None:
        rc = mon.state.returncode or 0

    state = mon.state
    print()
    if rc == 0:
        print(S.green(f"✓ merged {len(state.completed)} package(s) "
                      f"in {int(state.elapsed)}s"))
    else:
        print(S.red(f"✗ emerge exited {rc}"))
        if state.failed:
            for cpv in state.failed:
                print(S.red(f"  failed: {cpv}"))
        print(S.dim(f"  full log: {log_path}"))
        _explain_failure(log_path)
    if state.peak_rss():
        print(S.dim(f"  peak build memory {state.peak_rss() / (1 << 20):.0f} MiB"))
    return rc


def _explain_failure(log_path: str) -> None:
    """Surface the useful lines from a failed build instead of the whole log."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    interesting = [
        line
        for line in lines
        if any(
            marker in line
            for marker in ("!!! ERROR", "error:", " * ERROR:", "died", "Call stack")
        )
    ]
    if interesting:
        print(S.dim("  last errors:"))
        for line in interesting[-5:]:
            print(S.dim(f"    {line.strip()[:150]}"))


def cmd_search(args) -> int:
    matches = completion.complete(args.query, installed_only=args.installed, limit=args.limit)
    if not matches:
        print(S.dim("no matches"))
        return 1
    for m in matches:
        marker = S.green("I") if m.installed else " "
        print(f"{marker} {S.bold(m.text):<48} {S.dim(m.description)}")
    print(S.dim(f"\n{len(matches)} match(es)"))
    return 0


def cmd_info(args) -> int:
    pkg = completion.lookup(args.package)
    if pkg is None:
        matches = completion.complete(args.package, limit=5)
        print(S.red(f"no exact match for {args.package}"))
        if matches:
            print(S.dim("did you mean:"))
            for m in matches:
                print(S.dim(f"  {m.text}"))
        return 1

    print(S.bold(pkg.cp))
    print(f"  {pkg.description}")
    print()
    print(f"  {S.dim('best version'):<24} {pkg.version}"
          f"{'' if pkg.is_stable else S.yellow('  (~amd64, needs keywording)')}")
    print(f"  {S.dim('installed'):<24} {pkg.installed or S.dim('no')}")
    print(f"  {S.dim('slot'):<24} {pkg.slot}")
    if pkg.required_use:
        print(f"  {S.dim('REQUIRED_USE'):<24} {pkg.required_use[:110]}")

    enabled = index.installed_use().get(pkg.cp, set())
    defaults = pkg.default_on
    flags = pkg.use_flags
    if flags:
        print()
        print(S.dim("  USE flags:"))
        for flag in sorted(flags):
            if flag in enabled:
                mark, colour = "+", S.green
            elif flag in defaults:
                mark, colour = "+", S.yellow  # default-on but not installed that way
            else:
                mark, colour = "-", S.dim
            desc = scope.flag_description(flag, pkg.cp)
            print(f"    {colour(mark + flag):<28} {S.dim(desc[:80])}")
    return 0


def cmd_complete(args) -> int:
    return completion.main(args.rest)


def cmd_index(args) -> int:
    if args.refresh or index.is_stale():
        print(S.dim("building package index…"), file=sys.stderr)
        count, path = index.refresh(progress=sys.stderr.isatty())
        print(S.green(f"indexed {count} packages → {path}"))
    else:
        packages = index.load(rebuild_if_stale=False)
        print(S.green(f"index is current: {len(packages)} packages"))
        print(S.dim(f"  {index.index_path()}"))
    return 0


def cmd_update(args) -> int:
    args.atoms = ["@world"]
    args.emerge_opts = ["--update", "--deep", "--newuse"] + list(args.emerge_opts)
    return cmd_install(args)


def cmd_tidy(args) -> int:
    """Repair the managed package.use file and fold in any pre-rename file."""
    lines = scope.read_managed()
    if not lines:
        print(S.dim("nothing to tidy"))
        return 0

    removed: list[str] = []
    tidied = scope.tidy_lines(lines, removed=removed)
    legacy_present = os.path.exists(scope.LEGACY_MANAGED_FILE)

    if not removed and not legacy_present:
        print(S.green("managed config is already consistent"))
        return 0

    if removed:
        print(S.yellow("These entries were shadowing a */* wildcard and did nothing:"))
        for entry in removed:
            print(f"  {entry}")
        print(
            S.dim(
                "\nPortage ranks package.use by atom specificity, not file order, so a\n"
                "per-package line always beats */* no matter where it appears."
            )
        )
    if legacy_present:
        print(S.dim(f"\nWill fold in and remove {scope.LEGACY_MANAGED_FILE}"))

    if args.pretend:
        print(S.dim("\n--pretend: nothing written"))
        return 0

    content = "\n".join(tidied).rstrip() + "\n"
    if not prompts._write_privileged(scope.MANAGED_FILE, content):
        print(S.red("could not write the managed file"))
        return 1
    if legacy_present:
        prompts._remove_privileged(scope.LEGACY_MANAGED_FILE)
    print(S.green(f"\ntidied {scope.MANAGED_FILE}"))
    return 0


def cmd_changes(args) -> int:
    """Show what eh has written to /etc/portage."""
    any_shown = False
    legacy_seen = []
    candidates = [
        ("USE flags", scope.MANAGED_FILE),
        ("keywords", prompts.KEYWORDS_FILE),
        ("licenses", prompts.LICENSE_FILE),
        ("USE flags (pre-rename)", scope.LEGACY_MANAGED_FILE),
        ("keywords (pre-rename)", prompts.LEGACY_FILES[prompts.KEYWORDS_FILE]),
        ("licenses (pre-rename)", prompts.LEGACY_FILES[prompts.LICENSE_FILE]),
    ]
    for label, path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read().rstrip()
        except OSError:
            continue
        entries = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
        if not entries:
            continue
        any_shown = True
        if "pre-rename" in label:
            legacy_seen.append(path)
        print(S.bold(f"{label}  ") + S.dim(path))
        for line in entries:
            print(f"  {line}")
        print()
    if not any_shown:
        print(S.dim("eh hasn't written any configuration changes yet"))
    elif legacy_seen:
        print(S.yellow("Files above marked 'pre-rename' were written as zz-portagehelper."))
        print(S.dim("They still work — Portage reads every file in these directories."))
        print(S.dim("Run ./install.sh to rename them to zz-emergehelper."))
    return 0


# --- argument parsing ------------------------------------------------------


# Short-flag spellings of the subcommands: `eh -i emacs` == `eh install emacs`.
# The flag may appear anywhere, so `eh -i emacs -y` works too.
ACTION_FLAGS = {
    "-i": "install",
    "--install": "install",
    "-u": "update",
    "--update": "update",
    "-s": "search",
    "--search": "search",
    "-Q": "info",
    "--info": "info",
    "-x": "index",
    "--index": "index",
    "-L": "changes",
    "--changes": "changes",
    "-T": "tidy",
    "--tidy": "tidy",
}

SUBCOMMANDS = {
    "install", "i", "update", "u", "search", "s",
    "info", "index", "changes", "complete", "tidy",
}


def normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite an action flag into the subcommand form argparse expects."""
    if not argv or argv[0] in SUBCOMMANDS:
        return argv
    for i, token in enumerate(argv):
        if token in ACTION_FLAGS:
            return [ACTION_FLAGS[token]] + argv[:i] + argv[i + 1 :]
        # Stop at "--": everything after it is a value, not our flag.
        if token == "--":
            break
    return argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eh",
        description="EmergeHelper — a friendlier front-end for Portage.",
        epilog=(
            "Actions can be given as a flag or a word: "
            "'eh -i emacs' == 'eh install emacs'.\n"
            "  -i/--install  -u/--update  -s/--search  "
            "-Q/--info  -x/--index  -L/--changes  -T/--tidy"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"eh {VERSION}")
    sub = parser.add_subparsers(dest="command")

    def add_merge_flags(p):
        p.add_argument("-p", "--pretend", action="store_true",
                       help="resolve and show changes, write and merge nothing")
        p.add_argument("-y", "--yes", action="store_true",
                       help="accept the recommended scope for every change")
        p.add_argument("--plain", action="store_true",
                       help="run emerge directly, without the progress UI")
        p.add_argument("--no-binpkg", action="store_true",
                       help="ignore binary packages when resolving")
        p.add_argument("-o", "--emerge-opt", dest="emerge_opts", action="append",
                       default=[], metavar="OPT",
                       help="extra option passed through to emerge (repeatable)")

    p_install = sub.add_parser("install", aliases=["i"], help="merge packages")
    p_install.add_argument("atoms", nargs="*")
    add_merge_flags(p_install)
    p_install.set_defaults(func=cmd_install)

    p_update = sub.add_parser("update", aliases=["u"], help="update @world")
    p_update.add_argument("atoms", nargs="*", default=[])
    add_merge_flags(p_update)
    p_update.set_defaults(func=cmd_update)

    p_search = sub.add_parser("search", aliases=["s"], help="search packages")
    p_search.add_argument("query")
    p_search.add_argument("-I", "--installed", action="store_true")
    p_search.add_argument("-n", "--limit", type=int, default=40)
    p_search.set_defaults(func=cmd_search)

    p_info = sub.add_parser("info", help="show a package and its USE flags")
    p_info.add_argument("package")
    p_info.set_defaults(func=cmd_info)

    p_index = sub.add_parser("index", help="inspect or rebuild the package index")
    p_index.add_argument("--refresh", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_changes = sub.add_parser("changes", help="show config eh has written")
    p_changes.set_defaults(func=cmd_changes)

    p_tidy = sub.add_parser("tidy", help="repair the managed package.use file")
    p_tidy.add_argument("-p", "--pretend", action="store_true",
                        help="show what would change, write nothing")
    p_tidy.set_defaults(func=cmd_tidy)

    p_complete = sub.add_parser("complete", help=argparse.SUPPRESS)
    p_complete.add_argument("rest", nargs=argparse.REMAINDER)
    p_complete.set_defaults(func=cmd_complete)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = normalize_argv(list(sys.argv[1:] if argv is None else argv))
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(S.dim("\ninterrupted"))
        return 130


if __name__ == "__main__":
    sys.exit(main())
