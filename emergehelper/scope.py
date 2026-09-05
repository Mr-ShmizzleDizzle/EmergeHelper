"""Work out *where* a USE-flag change should be written, and what it would cost.

The core question this answers: if you set a flag globally instead of on one
package, what else on the system changes, and does anything actually break?
Breakage is decided by Portage's own ``check_required_use``, so a reported
conflict is a real REQUIRED_USE violation rather than a guess.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import index

# All eh-managed flag changes land in one file. It sorts last in
# /etc/portage/package.use, so its entries win over the hand-written files,
# and everything eh ever did can be reviewed or reverted in one place.
MANAGED_FILE = "/etc/portage/package.use/zz-emergehelper"
# Written by the tool under its previous name. Still read so entries made
# before the rename keep counting; install.sh offers to rename it.
LEGACY_MANAGED_FILE = "/etc/portage/package.use/zz-portagehelper"

SCOPE_VERSION = "version"
SCOPE_PACKAGE = "package"
SCOPE_GLOBAL = "global"


@dataclass(slots=True)
class FlagChange:
    """A single USE-flag change Portage says it needs."""

    cp: str
    version: str = ""  # empty when the requirement isn't version-pinned
    flag: str = ""
    enable: bool = True
    required_by: str = ""  # the package that pulled this requirement in
    # Portage suggested this only to break a circular dependency; it says such
    # a change can be reverted once the package is installed.
    cycle_breaker: bool = False

    @property
    def token(self) -> str:
        """The flag as written in package.use: ``postproc`` or ``-postproc``."""
        return self.flag if self.enable else f"-{self.flag}"

    def atom(self, scope: str) -> str:
        if scope == SCOPE_VERSION and self.version:
            return f"={self.cp}-{self.version}"
        if scope == SCOPE_GLOBAL:
            return "*/*"
        return self.cp


@dataclass(slots=True)
class Conflict:
    """An installed package that a global setting would actually break."""

    cp: str
    reason: str


@dataclass(slots=True)
class ScopeOption:
    """One place the change could be written, with its consequences."""

    scope: str
    atom: str
    token: str
    label: str
    rebuilds: int = 0  # installed packages whose USE would change
    conflicts: list[Conflict] = field(default_factory=list)
    recommended: bool = False

    @property
    def line(self) -> str:
        """The literal package.use line this option would write."""
        return f"{self.atom} {self.token}"

    @property
    def safe(self) -> bool:
        return not self.conflicts


@dataclass(slots=True)
class Analysis:
    """Everything the prompt needs to explain a change to the user."""

    change: FlagChange
    options: list[ScopeOption]
    tree_matches: int  # packages in the tree exposing this flag
    installed_matches: int  # installed packages exposing this flag
    description: str = ""  # flag meaning, from use.desc
    companions: list[str] = field(default_factory=list)

    @property
    def recommended(self) -> ScopeOption:
        for o in self.options:
            if o.recommended:
                return o
        return self.options[0]

    @property
    def blocked(self) -> bool:
        """True when the recommended change alone won't satisfy REQUIRED_USE."""
        return bool(self.recommended.conflicts)


_CONFIG = None


def _portage_config():
    """Lazily build a private Portage config clone plus the tree dbapi.

    Cloned because ``setcpv`` mutates the settings object, and we must not
    disturb the caller's global Portage state.
    """
    global _CONFIG
    if _CONFIG is None:
        import portage

        settings = portage.config(clone=portage.settings)
        _CONFIG = (settings, portage.db[portage.root]["porttree"].dbapi)
    return _CONFIG


_EFFECTIVE_CACHE: dict[str, set[str]] = {}


def effective_use(cp: str) -> set[str] | None:
    """The USE flags a package *would* be built with right now.

    For an uninstalled package the vdb has nothing to tell us, so ask Portage
    to resolve profile defaults, make.conf and package.use the same way emerge
    would.  Without this, every uninstalled package looks like it has no flags
    set and REQUIRED_USE checks report conflicts that don't exist.
    """
    if cp in _EFFECTIVE_CACHE:
        return _EFFECTIVE_CACHE[cp]
    try:
        settings, tree = _portage_config()
        matches = tree.cp_list(cp)
        if not matches:
            return None
        cpv = matches[-1]
        settings.setcpv(cpv, mydb=tree)
        use = set(settings["PORTAGE_USE"].split())
    except Exception:
        return None
    _EFFECTIVE_CACHE[cp] = use
    return use


def _baseline(pkg: index.Package, current_use: dict[str, set[str]]) -> set[str]:
    """Current flag set for a package: from the vdb if installed, else resolved."""
    if pkg.is_installed and pkg.cp in current_use:
        return current_use[pkg.cp]
    resolved = effective_use(pkg.cp)
    if resolved is not None:
        return resolved
    # Last resort: the ebuild's own IUSE defaults.
    return pkg.default_on


def _iuse_matcher(pkg: index.Package):
    flags = set(pkg.use_flags)
    return lambda f: f in flags


def _violates(pkg: index.Package, use: set[str]) -> bool:
    """True when ``use`` breaks the package's REQUIRED_USE constraints."""
    if not pkg.required_use:
        return False
    try:
        from portage.dep import check_required_use

        return not check_required_use(pkg.required_use, use, _iuse_matcher(pkg))
    except Exception:
        # If Portage can't parse it, don't invent a conflict.
        return False


def analyze(
    change: FlagChange,
    packages: list[index.Package] | None = None,
    current_use: dict[str, set[str]] | None = None,
) -> Analysis:
    """Score every scope option for one flag change."""
    if packages is None:
        packages = index.load()
    if current_use is None:
        current_use = index.installed_use()

    by_cp = {p.cp: p for p in packages}
    target = by_cp.get(change.cp)

    # Every package in the tree that even has this flag.
    exposing = [p for p in packages if p.has_flag(change.flag)]
    installed_exposing = [p for p in exposing if p.is_installed]

    # What a global "*/* flag" would do to already-installed packages.
    rebuilds = 0
    conflicts: list[Conflict] = []
    for p in installed_exposing:
        now = current_use.get(p.cp, set())
        if change.enable:
            if change.flag in now:
                continue  # already on; nothing changes
            new = now | {change.flag}
        else:
            if change.flag not in now:
                continue
            new = now - {change.flag}
        rebuilds += 1
        if _violates(p, new):
            conflicts.append(
                Conflict(cp=p.cp, reason=f"REQUIRED_USE: {p.required_use.strip()[:90]}")
            )

    options: list[ScopeOption] = []

    if change.version:
        options.append(
            ScopeOption(
                scope=SCOPE_VERSION,
                atom=change.atom(SCOPE_VERSION),
                token=change.token,
                label="this version only",
                rebuilds=1,
            )
        )

    # Does the target package itself survive the change?
    package_conflicts: list[Conflict] = []
    if target is not None:
        now = _baseline(target, current_use)
        new = (now | {change.flag}) if change.enable else (now - {change.flag})
        if _violates(target, new):
            package_conflicts.append(
                Conflict(
                    cp=target.cp,
                    reason=(
                        "this flag alone doesn't satisfy REQUIRED_USE: "
                        f"{target.required_use.strip()[:90]}"
                    ),
                )
            )

    options.append(
        ScopeOption(
            scope=SCOPE_PACKAGE,
            atom=change.atom(SCOPE_PACKAGE),
            token=change.token,
            label="this package, all versions",
            rebuilds=1,
            conflicts=package_conflicts,
        )
    )

    options.append(
        ScopeOption(
            scope=SCOPE_GLOBAL,
            atom="*/*",
            token=change.token,
            label="every package on the system",
            rebuilds=rebuilds,
            conflicts=conflicts,
        )
    )

    # Package scope is the default answer: version scope breaks on the next
    # upgrade, and global scope is almost never what one dependency needs.
    #
    # Note a REQUIRED_USE violation is NOT a reason to widen the scope --
    # setting the same flag on more packages cannot satisfy a constraint on
    # this one. The fix there is companion flags, so we keep recommending the
    # narrow scope and surface what else needs enabling.
    package_option = next(o for o in options if o.scope == SCOPE_PACKAGE)
    package_option.recommended = True

    companions: list[str] = []
    if package_conflicts and target is not None:
        companions = suggest_companions(target, change, current_use)

    return Analysis(
        change=change,
        options=options,
        tree_matches=len(exposing),
        installed_matches=len(installed_exposing),
        description=flag_description(change.flag, change.cp),
        companions=companions,
    )


def suggest_companions(
    pkg: index.Package,
    change: FlagChange,
    current_use: dict[str, set[str]] | None = None,
) -> list[str]:
    """Find extra flags that would satisfy REQUIRED_USE alongside the change.

    Brute-forces one and then two extra flags. IUSE is small enough (tens of
    flags) that this stays fast, and it turns "that won't work" into an
    actionable "also enable X".
    """
    if current_use is None:
        current_use = index.installed_use()
    base = _baseline(pkg, current_use)
    target_use = (base | {change.flag}) if change.enable else (base - {change.flag})
    candidates = [f for f in pkg.use_flags if f != change.flag]

    # Single additional flag, toggled either way.
    for f in candidates:
        for enable in (True, False):
            trial = (target_use | {f}) if enable else (target_use - {f})
            if trial == target_use:
                continue
            if not _violates(pkg, trial):
                return [f if enable else f"-{f}"]

    # Fall back to pairs, capped so a huge IUSE can't stall the prompt.
    for i, a in enumerate(candidates[:40]):
        for b in candidates[i + 1 : 40]:
            trial = target_use | {a, b}
            if not _violates(pkg, trial):
                return [a, b]
    return []


_USE_DESC_CACHE: dict[str, str] | None = None


def flag_description(flag: str, cp: str = "") -> str:
    """Look up a flag's meaning in the profile's use.desc / use.local.desc."""
    global _USE_DESC_CACHE
    if _USE_DESC_CACHE is None:
        _USE_DESC_CACHE = {}
        profiles = os.path.join(index.PORTDIR, "profiles")
        for name in ("use.desc", "use.local.desc"):
            path = os.path.join(profiles, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.startswith("#") or " - " not in line:
                            continue
                        key, _, desc = line.partition(" - ")
                        _USE_DESC_CACHE[key.strip()] = desc.strip()
            except OSError:
                continue

        # USE_EXPAND flags (abi_x86_32, python_targets_python3_14, video_cards_*)
        # are described in profiles/desc/<group>.desc, keyed by the suffix only:
        # "32 - 32-bit (x86) libraries" in abi_x86.desc means abi_x86_32.
        desc_dir = os.path.join(profiles, "desc")
        try:
            desc_files = os.listdir(desc_dir)
        except OSError:
            desc_files = []
        for filename in desc_files:
            if not filename.endswith(".desc"):
                continue
            group = filename[: -len(".desc")]
            try:
                with open(os.path.join(desc_dir, filename), encoding="utf-8",
                          errors="replace") as fh:
                    for line in fh:
                        if line.startswith("#") or " - " not in line:
                            continue
                        key, _, desc = line.partition(" - ")
                        _USE_DESC_CACHE.setdefault(
                            f"{group}_{key.strip()}", desc.strip()
                        )
            except OSError:
                continue
    # Package-specific descriptions are keyed "cat/pkg:flag".
    if cp:
        local = _USE_DESC_CACHE.get(f"{cp}:{flag}")
        if local:
            return local
    return _USE_DESC_CACHE.get(flag, "")


def read_managed() -> list[str]:
    """Current contents of the eh-managed package.use file.

    Falls back to the pre-rename filename so existing entries are carried
    forward the next time we write.
    """
    combined: list[str] = []
    seen_atoms: set[str] = set()
    # Legacy first so the current file's entries win on conflict. Both are
    # live as far as Portage is concerned, so a stale line in the old file
    # can still override the new one - we have to fold them together.
    for path in (LEGACY_MANAGED_FILE, MANAGED_FILE):
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for line in lines:
            atom = _line_atom(line)
            if not atom:
                if line not in combined:
                    combined.append(line)
                continue
            if atom in seen_atoms:
                # Replace the earlier (legacy) entry with this one.
                combined = [l for l in combined if _line_atom(l) != atom]
            seen_atoms.add(atom)
            combined.append(line)
    return combined


def tidy_lines(lines: list[str], removed: list[str] | None = None) -> list[str]:
    """Make every wildcard entry in the file actually take effect.

    A ``*/* foo`` line does nothing for a package that also has its own
    ``cat/pkg -foo`` entry, because Portage ranks by atom specificity rather
    than file order. This clears those shadowing entries so the file means
    what it looks like it means.
    """
    wildcard_flags: set[str] = set()
    for line in lines:
        if _line_atom(line) != "*/*":
            continue
        body = line.split("#", 1)[0].split()
        wildcard_flags.update(t.lstrip("-") for t in body[1:])
    if not wildcard_flags:
        return list(lines)
    return _clear_flags_from_specific(lines, wildcard_flags, removed)


def managed_flag_packages(flag: str) -> set[str]:
    """Packages the managed file already sets ``flag`` on.

    A flag that keeps reappearing for package after package (``abi_x86_32``
    for a 32-bit stack is the classic) is a sign the per-package scope is the
    wrong tool. Counting what we've already written lets the prompt say so
    instead of asking the same question twenty times.
    """
    out: set[str] = set()
    for line in read_managed():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        atom, flags = parts[0], parts[1:]
        if any(f.lstrip("-") == flag for f in flags):
            out.add(atom.lstrip("<>=~!").split(":")[0])
    return out


def managed_flag_state(cp: str, flag: str) -> bool | None:
    """How the managed file currently sets ``flag`` for ``cp``.

    True for enabled, False for disabled, None if we've never written it.
    Used to warn before silently reversing an earlier decision.
    """
    for line in read_managed():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        atom = parts[0].lstrip("<>=~!").split(":")[0]
        # Strip any trailing version so "=cat/pkg-1.2" matches "cat/pkg".
        if atom != cp:
            base = atom.rsplit("-", 1)[0] if "-" in atom else atom
            if base != cp:
                continue
        for token in parts[1:]:
            if token.lstrip("-") == flag:
                return not token.startswith("-")
    return None


def render_addition(option: ScopeOption, change: FlagChange) -> str:
    """The commented line eh would append for this choice."""
    why = f"  # for {change.required_by}" if change.required_by else ""
    return f"{option.line}{why}"


def _line_atom(line: str) -> str:
    """The atom a managed line applies to, or "" for blanks and comments."""
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return ""
    return stripped.split()[0]


def _clear_flags_from_specific(
    lines: list[str],
    flags: set[str],
    removed: list[str] | None = None,
) -> list[str]:
    """Drop ``flags`` from every non-wildcard entry.

    Portage resolves package.use by atom *specificity*, not file order, so
    ``media-libs/mesa -abi_x86_32`` beats a later ``*/* abi_x86_32`` and the
    global setting silently does nothing. Writing a wildcard therefore has to
    clear the same flag from the more specific lines it is meant to supersede.
    """
    out: list[str] = []
    for line in lines:
        atom = _line_atom(line)
        if not atom or atom == "*/*":
            out.append(line)
            continue
        body, sep, comment = line.partition("#")
        tokens = body.split()
        kept = [t for t in tokens[1:] if t.lstrip("-") not in flags]
        if len(kept) == len(tokens) - 1:
            out.append(line)
            continue
        if removed is not None:
            dropped = [t for t in tokens[1:] if t.lstrip("-") in flags]
            removed.append(f"{atom} {' '.join(dropped)}")
        if not kept:
            continue  # nothing left on that line; drop it entirely
        out.append(f"{atom} {' '.join(kept)}" + (f"  #{comment}" if sep else ""))
    return out


def merge_lines(
    existing: list[str],
    additions: list[str],
    removed: list[str] | None = None,
) -> list[str]:
    """Fold new entries into the managed file, replacing same-atom duplicates.

    Keeps the file from growing a new line every time the same package needs
    the same flag adjusted again, and keeps a wildcard entry from being
    quietly outranked by a leftover per-package one.
    """
    out = list(existing)
    if not out:
        out = [
            "# Managed by EmergeHelper (eh). Safe to edit or delete by hand.",
            "# Each line records which package required the change.",
            "",
        ]

    for addition in additions:
        # A wildcard only takes effect once the specific entries it supersedes
        # stop overriding it.
        if _line_atom(addition) == "*/*":
            body = addition.split("#", 1)[0].split()
            out = _clear_flags_from_specific(
                out, {t.lstrip("-") for t in body[1:]}, removed
            )

    for addition in additions:
        atom, _, rest = addition.partition(" ")
        new_flag = rest.split("#", 1)[0].strip()
        replaced = False
        for i, line in enumerate(out):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.split()[0] != atom:
                continue
            body, _, comment = line.partition("#")
            flags = body.split()[1:]
            bare = new_flag.lstrip("-")
            # Drop any prior setting of this same flag, then re-add it.
            flags = [f for f in flags if f.lstrip("-") != bare]
            flags.append(new_flag)
            out[i] = f"{atom} {' '.join(flags)}" + (f"  #{comment}" if comment else "")
            replaced = True
            break
        if not replaced:
            out.append(addition)
    return out
