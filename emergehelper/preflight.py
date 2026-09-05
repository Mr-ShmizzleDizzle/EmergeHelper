"""Resolve a merge before running it, and collect what Portage says it needs.

Everything here happens in a throwaway copy of /etc/portage, so a preflight
never needs root and never touches real config.  We deliberately parse
Portage's *reported* changes rather than letting ``--autounmask-write`` apply
them: emerge refuses to write under ``--pretend`` anyway, and reporting keeps
the decision with the user, which is the whole point of the flag prompt.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from .scope import FlagChange

# Config entries worth copying into the sandbox. gnupg is skipped: it is
# root-only and irrelevant to dependency resolution.
_COPY = (
    "make.conf",
    "package.use",
    "package.accept_keywords",
    "package.keywords",
    "package.license",
    "package.mask",
    "package.unmask",
    "package.env",
    "env",
    "profile",
    "sets",
    "binrepos.conf",
    "repos.conf",
    "savedconfig",
)

# "- media-libs/libvpx-1.16.0::gentoo (Change USE: +postproc -foo)"
_CHANGE_USE = re.compile(
    r"^-\s+(?P<cpv>[\w.+-]+/[\w.+-]+)(?:::(?P<repo>\S+))?\s+\(Change USE:\s+(?P<flags>[^)]+)\)"
)
# '(dependency required by "www-client/firefox-140.15.0::gentoo[...]" [ebuild])'
_REQUIRED_BY = re.compile(r'dependency required by "(?P<atom>[^"\s]+)')
# "...violates use flag constraints defined by <cpv>: 'exactly-one-of ( ... )'"
_VIOLATES = re.compile(r"violates use flag constraints defined by\s+(?P<cpv>\S+):\s+'(?P<rule>.*)'")
# Header of an autounmask block, e.g. "The following USE changes are necessary"
_BLOCK = re.compile(r"^The following (?P<kind>\w[\w ]*?) changes are necessary")
# "- games-util/steam-launcher-1.0.0.87::steam-overlay (masked by: ~amd64 keyword)"
# Emitted when autounmask can't or won't propose a fix itself; without this
# the whole request looks like an unexplained dead end.
# The reason text itself contains parentheses ("EULA license(s)"), so match
# greedily to the final ")" rather than the first.
_MASKED = re.compile(
    r"^-\s+(?P<cpv>[\w.+-]+/[\w.+-]+?)(?:::(?P<repo>\S+))?\s+\(masked by:\s+(?P<reasons>.+)\)\s*$"
)
# "~amd64 keyword", "missing keyword", "** keyword"
_KEYWORD_REASON = re.compile(r"^(?P<kw>\S+)\s+keyword$")
# A config line inside such a block: "=app-admin/conky-1.24.2 ~amd64"
_ATOM_LINE = re.compile(r"^(?P<atom>[<>=~!]*[\w.+*-]+/[\w.+*-]+\S*)\s+(?P<rest>.+)$")


@dataclass(slots=True)
class KeywordChange:
    atom: str
    keyword: str
    required_by: str = ""


@dataclass(slots=True)
class LicenseChange:
    atom: str
    license: str
    required_by: str = ""


@dataclass(slots=True)
class MaskedPackage:
    """A candidate Portage rejected, and why."""

    cpv: str
    repo: str
    reasons: list[str]

    @property
    def hard_masked(self) -> bool:
        """package.mask entries mean a maintainer marked this broken/unsafe."""
        return any("package.mask" in r for r in self.reasons)


@dataclass(slots=True)
class Preflight:
    """Outcome of a dry-run resolve."""

    resolved: bool  # emerge produced a usable plan
    use_changes: list[FlagChange] = field(default_factory=list)
    keyword_changes: list[KeywordChange] = field(default_factory=list)
    license_changes: list[LicenseChange] = field(default_factory=list)
    violations: list[tuple[str, str]] = field(default_factory=list)  # (cpv, rule)
    masked: list[MaskedPackage] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)  # cpvs in the merge plan
    raw: str = ""
    returncode: int = 0

    @property
    def needs_changes(self) -> bool:
        return bool(self.use_changes or self.keyword_changes or self.license_changes)

    @property
    def clean(self) -> bool:
        """Nothing to ask about: emerge can proceed as-is."""
        return self.resolved and not self.needs_changes and not self.blockers


def split_cpv(cpv: str) -> tuple[str, str]:
    """``media-libs/libvpx-1.16.0`` -> ``("media-libs/libvpx", "1.16.0")``."""
    try:
        from portage.versions import pkgsplit, catsplit

        category, name = catsplit(cpv)
        parts = pkgsplit(name)
        if parts:
            pn, ver, rev = parts
            return f"{category}/{pn}", (ver if rev == "r0" else f"{ver}-{rev}")
    except Exception:
        pass
    for i in range(len(cpv) - 1, 0, -1):
        if cpv[i] == "-" and cpv[i + 1 : i + 2].isdigit():
            return cpv[:i], cpv[i + 1 :]
    return cpv, ""


def make_sandbox(base: str = "/etc/portage") -> str:
    """Copy the Portage config into a temp dir usable as PORTAGE_CONFIGROOT."""
    root = tempfile.mkdtemp(prefix="emergehelper-cfg-")
    portage_dir = os.path.join(root, "etc", "portage")
    os.makedirs(portage_dir, exist_ok=True)

    for name in _COPY:
        src = os.path.join(base, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(portage_dir, name)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        except (OSError, shutil.Error):
            # Unreadable fragments (root-only files) simply don't take part.
            continue

    # make.profile is a *relative* symlink; re-point it absolutely so it still
    # resolves from the sandbox location.
    profile = os.path.join(base, "make.profile")
    if os.path.exists(profile):
        target = os.path.realpath(profile)
        link = os.path.join(portage_dir, "make.profile")
        try:
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(target, link)
        except OSError:
            pass
    return root


def _strip_noise(text: str) -> str:
    keep = []
    for line in text.splitlines():
        if "news item" in line or "eselect news" in line:
            continue
        keep.append(line)
    return "\n".join(keep)


def parse(output: str) -> Preflight:
    """Turn emerge's resolver output into structured change requests."""
    result = Preflight(resolved=True, raw=output)
    lines = output.splitlines()

    block_kind: str | None = None
    pending_required_by = ""
    breaking_cycle = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Changes suggested purely to break a dependency cycle are temporary;
        # Portage says so itself right after listing them.
        if "possible to break this cycle" in stripped:
            breaking_cycle = True
            continue
        if "can be reverted" in stripped:
            breaking_cycle = False
            continue

        # --- inline "(Change USE: ...)" form, the common modern shape --------
        m = _CHANGE_USE.match(stripped)
        if m:
            cp, version = split_cpv(m.group("cpv"))
            # Attribution lives on the following "(dependency required by ...)"
            # lines; the first one is the direct requirer.
            required_by = ""
            for follow in lines[i + 1 : i + 5]:
                rm = _REQUIRED_BY.search(follow)
                if rm:
                    required_by = rm.group("atom").split("::")[0]
                    break
            for token in m.group("flags").split():
                if token.startswith(("+", "-")):
                    result.use_changes.append(
                        FlagChange(
                            cp=cp,
                            version=version,
                            flag=token[1:],
                            enable=token[0] == "+",
                            required_by=required_by,
                            cycle_breaker=breaking_cycle,
                        )
                    )
            continue

        mm = _MASKED.match(stripped)
        if mm:
            result.masked.append(
                MaskedPackage(
                    cpv=mm.group("cpv"),
                    repo=mm.group("repo") or "",
                    reasons=[r.strip() for r in mm.group("reasons").split(",")],
                )
            )
            continue

        vm = _VIOLATES.search(stripped)
        if vm:
            result.violations.append((vm.group("cpv"), vm.group("rule")))
            continue

        # --- block form: "The following USE changes are necessary..." -------
        bm = _BLOCK.match(stripped)
        if bm:
            block_kind = bm.group("kind").strip().lower()
            pending_required_by = ""
            continue

        if block_kind:
            if not stripped:
                block_kind = None
                continue
            if stripped.startswith("#"):
                # "# required by www-client/firefox-140.15.0 (argument)"
                if "required by" in stripped:
                    rest = stripped.split("required by", 1)[1].strip()
                    pending_required_by = rest.split(" ")[0].split("::")[0]
                continue
            if stripped.startswith("("):
                continue

            am = _ATOM_LINE.match(stripped)
            if not am:
                block_kind = None
                continue
            atom, rest = am.group("atom"), am.group("rest").strip()

            if "use" in block_kind:
                bare = atom.lstrip("<>=~!")
                cp, version = split_cpv(bare)
                if not version:
                    cp, version = bare, ""
                for token in rest.split():
                    flag = token.lstrip("+-")
                    if flag:
                        result.use_changes.append(
                            FlagChange(
                                cp=cp,
                                version=version,
                                flag=flag,
                                enable=not token.startswith("-"),
                                required_by=pending_required_by,
                            )
                        )
            elif "keyword" in block_kind:
                result.keyword_changes.append(
                    KeywordChange(atom=atom, keyword=rest, required_by=pending_required_by)
                )
            elif "license" in block_kind:
                result.license_changes.append(
                    LicenseChange(atom=atom, license=rest, required_by=pending_required_by)
                )
            continue

        # --- merge plan and hard failures -----------------------------------
        if stripped.startswith("[ebuild") or stripped.startswith("[binary"):
            tail = stripped.split("]", 1)[-1].strip()
            cpv = tail.split(" ")[0] if tail else ""
            if cpv:
                result.packages.append(cpv)
        elif stripped.startswith("!!!") and "One of the following" not in stripped:
            result.blockers.append(stripped.lstrip("! "))
        elif "Multiple package instances within a single package slot" in stripped:
            result.blockers.append(stripped)

    result.use_changes = _dedupe(result.use_changes)
    _promote_masked(result)
    return result


def _promote_masked(result: Preflight) -> None:
    """Turn "masked by: ~amd64 keyword" entries into offerable changes.

    autounmask usually proposes these itself, but not always - notably when a
    package exists only in an overlay, or when autounmask is disabled. Without
    this the user just sees "have been masked" and a dead end, which is the
    exact wall a plain emerge hits.

    Hard masks (package.mask) are deliberately *not* promoted: a maintainer
    marked those broken or unsafe, and quietly offering to unmask them would
    be the wrong default.
    """
    if not result.masked:
        return

    known_keywords = {k.atom for k in result.keyword_changes}
    known_licenses = {l.atom for l in result.license_changes}
    handled: set[str] = set()

    for entry in result.masked:
        cp, _ = split_cpv(entry.cpv)
        # Portage lists candidates newest first; one offer per package is
        # enough, and it should be the newest usable one.
        if cp in handled or entry.hard_masked:
            continue
        atom = f"={entry.cpv}"
        for reason in entry.reasons:
            km = _KEYWORD_REASON.match(reason)
            if km:
                keyword = km.group("kw")
                # "missing keyword" means no keyword for this arch at all;
                # "**" is the only thing that accepts it.
                if keyword == "missing":
                    keyword = "**"
                if atom not in known_keywords:
                    result.keyword_changes.append(
                        KeywordChange(atom=atom, keyword=keyword, required_by=cp)
                    )
                    known_keywords.add(atom)
                    handled.add(cp)
            elif "license" in reason:
                license_name = (
                    reason.replace("license(s)", "").replace("license", "").strip()
                )
                if license_name and atom not in known_licenses:
                    result.license_changes.append(
                        LicenseChange(atom=atom, license=license_name, required_by=cp)
                    )
                    known_licenses.add(atom)
                    handled.add(cp)


def _dedupe(changes: list[FlagChange]) -> list[FlagChange]:
    seen: dict[tuple[str, str, bool], FlagChange] = {}
    for c in changes:
        key = (c.cp, c.flag, c.enable)
        if key not in seen or (not seen[key].required_by and c.required_by):
            seen[key] = c
    return list(seen.values())


def run(
    atoms: list[str],
    emerge_opts: list[str] | None = None,
    timeout: int = 600,
    use_binpkg: bool = True,
) -> Preflight:
    """Dry-run a merge in a sandboxed config and report what it would need."""
    sandbox = make_sandbox()
    env = dict(os.environ)
    env["PORTAGE_CONFIGROOT"] = sandbox
    # Machine-readable output: no colour codes, no interactive prompts.
    env["NOCOLOR"] = "true"
    env["CLEAN_DELAY"] = "0"

    cmd = [
        "emerge",
        "--pretend",
        "--quiet",
        "--ask=n",
        "--autounmask=y",
        "--autounmask-only=y",
        "--autounmask-continue=n",
        "--color=n",
    ]
    if not use_binpkg:
        cmd.append("--getbinpkg=n")
    cmd.extend(emerge_opts or [])
    cmd.extend(atoms)

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = _strip_noise(proc.stdout + "\n" + proc.stderr)
        result = parse(output)
        result.returncode = proc.returncode
        # A non-zero exit with no parsed changes means we couldn't resolve.
        if proc.returncode != 0 and not result.needs_changes:
            result.resolved = False
            if not result.blockers:
                tail = [l for l in output.splitlines() if l.strip()][-3:]
                result.blockers = tail or ["emerge could not resolve this request"]
        return result
    except subprocess.TimeoutExpired:
        return Preflight(
            resolved=False,
            blockers=[f"dependency resolution timed out after {timeout}s"],
        )
    except FileNotFoundError:
        return Preflight(resolved=False, blockers=["emerge not found in PATH"])
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
