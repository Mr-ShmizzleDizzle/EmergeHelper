"""Fast package index built from Portage's md5-cache.

Portage's own metadata cache already holds everything we need (DESCRIPTION,
IUSE, KEYWORDS, SLOT) as flat ``KEY=value`` files, so we can build a complete
index with a plain directory walk instead of going through the (much slower)
dbapi for every package.  A full scan of ~33k ebuilds takes ~2.7s; the result
is cached so interactive lookups stay in the low milliseconds.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass

PORTDIR = "/var/db/repos/gentoo"
MD5_CACHE = os.path.join(PORTDIR, "metadata", "md5-cache")
# Bumped whenever the on-disk format changes, so stale caches are rebuilt.
INDEX_VERSION = "4"

# Live ebuilds are always masked and never what "the current version" means to
# someone browsing packages, so they're excluded from version selection.
_LIVE_VERSIONS = ("9999", "99999999", "999999")

# Fields we pull out of each md5-cache file. Anything else is skipped while
# scanning, which keeps the parse loop cheap.
_WANTED = ("DESCRIPTION", "IUSE", "KEYWORDS", "SLOT", "REQUIRED_USE")


@dataclass(slots=True)
class Package:
    """One ``category/name`` with its best available version."""

    cp: str
    version: str  # the version emerge would install today
    installed: str  # installed version, or "" if not installed
    slot: str
    description: str
    iuse: str  # space-separated, with +/- prefixes intact
    keywords: str
    required_use: str = ""
    latest: str = ""  # highest non-live version, stable or not
    repo: str = "gentoo"  # repository the best version comes from

    @property
    def is_overlay(self) -> bool:
        return self.repo not in ("", "gentoo")

    @property
    def category(self) -> str:
        return self.cp.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.cp.split("/", 1)[1]

    @property
    def is_installed(self) -> bool:
        return bool(self.installed)

    @property
    def use_flags(self) -> list[str]:
        """IUSE with the default-on ``+`` / default-off ``-`` markers stripped."""
        return [f.lstrip("+-") for f in self.iuse.split() if f.lstrip("+-")]

    @property
    def default_on(self) -> set[str]:
        """Flags the ebuild enables by default (the ``+foo`` entries in IUSE)."""
        return {f[1:] for f in self.iuse.split() if f.startswith("+") and len(f) > 1}

    def has_flag(self, flag: str) -> bool:
        return flag in self.use_flags

    @property
    def is_stable(self) -> bool:
        """True when the best version is stable on this arch (no ``~`` prefix)."""
        return "amd64" in self.keywords.split()


@dataclass(slots=True)
class Repository:
    name: str
    location: str

    @property
    def md5_cache(self) -> str:
        return os.path.join(self.location, "metadata", "md5-cache")

    @property
    def has_md5_cache(self) -> bool:
        return os.path.isdir(self.md5_cache)


def repositories() -> list[Repository]:
    """Every configured ebuild repository, lowest priority first.

    Overlays must be indexed too - without this, their packages are invisible
    to completion and to the REQUIRED_USE analysis. Portage iterates
    ``settings.repositories`` in ascending priority order, which is exactly
    the order we want: later repos overwrite earlier ones, matching how
    Portage itself resolves a package present in several repos.
    """
    try:
        import portage

        repos = portage.db[portage.root]["porttree"].dbapi.settings.repositories
        found = [
            Repository(name=r.name, location=r.location)
            for r in repos
            if r.location and os.path.isdir(r.location)
        ]
        if found:
            return found
    except Exception:
        pass
    # Fall back to the main tree so a broken config still yields completions.
    return [Repository(name="gentoo", location=PORTDIR)]


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "emergehelper")


def index_path() -> str:
    return os.path.join(cache_dir(), "packages.tsv")


def _tree_stamp(locations: list[str] | None = None) -> str:
    """Identity of the current tree state, used to invalidate the cache.

    ``timestamp.chk`` changes on every ``emerge --sync``; the vdb mtime changes
    whenever a package is installed or removed, which is what keeps the
    ``[I]`` installed-markers honest between syncs.

    ``locations`` are the repository paths to watch. They are recorded in the
    index header at build time and read back on the staleness check, so this
    stays a handful of stat() calls - importing Portage here would add ~250ms
    to every Tab press.
    """
    watched = [
        os.path.join(PORTDIR, "metadata", "timestamp.chk"),
        "/var/db/pkg",
        "/var/lib/portage/world",
        # Adding or removing an overlay must invalidate the index too.
        "/etc/portage/repos.conf",
        "/var/db/repos",
    ]
    watched.extend(locations or [])
    parts = [INDEX_VERSION]
    for p in watched:
        try:
            parts.append(str(int(os.stat(p).st_mtime)))
        except OSError:
            parts.append("0")
    return "-".join(parts)


def _vercmp(a: str, b: str) -> int:
    """Compare two version strings, falling back to a plain string compare.

    Uses Portage's own comparator so ordering matches what emerge would pick.
    """
    try:
        from portage.versions import vercmp

        r = vercmp(a, b)
        if r is not None:
            return r
    except Exception:
        pass
    return (a > b) - (a < b)


def _split_cpv(filename: str, category: str) -> tuple[str, str] | None:
    """Split an md5-cache filename into ``(cp, version)``.

    md5-cache files are named ``<pkgname>-<version>``; the version always
    starts at the last ``-`` that is followed by a digit.
    """
    try:
        from portage.versions import pkgsplit

        parts = pkgsplit(filename)
        if parts:
            pn, ver, rev = parts
            full = ver if rev == "r0" else f"{ver}-{rev}"
            return f"{category}/{pn}", full
    except Exception:
        pass

    # Fallback: locate the version boundary by hand.
    for i in range(len(filename) - 1, 0, -1):
        if filename[i] == "-" and filename[i + 1 : i + 2].isdigit():
            return f"{category}/{filename[:i]}", filename[i + 1 :]
    return None


def _parse_metadata(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, val = line.partition("=")
                if sep and key in _WANTED:
                    out[key] = val.rstrip("\n")
                    if len(out) == len(_WANTED):
                        break
    except OSError:
        pass
    return out


def _installed_versions() -> dict[str, str]:
    """Map ``category/name`` -> installed version by reading the vdb directly."""
    installed: dict[str, str] = {}
    vdb = "/var/db/pkg"
    try:
        categories = os.listdir(vdb)
    except OSError:
        return installed
    for category in categories:
        cat_dir = os.path.join(vdb, category)
        try:
            entries = os.listdir(cat_dir)
        except OSError:
            continue
        for entry in entries:
            split = _split_cpv(entry, category)
            if not split:
                continue
            cp, version = split
            # Multiple slots can be installed; keep the highest for display.
            if cp not in installed or _vercmp(version, installed[cp]) > 0:
                installed[cp] = version
    return installed


def installed_use() -> dict[str, set[str]]:
    """Map ``category/name`` -> the USE flags currently enabled on disk.

    Read straight from the vdb, where each installed package records the exact
    flag set it was built with in its ``USE`` file.
    """
    out: dict[str, set[str]] = {}
    vdb = "/var/db/pkg"
    try:
        categories = os.listdir(vdb)
    except OSError:
        return out
    for category in categories:
        cat_dir = os.path.join(vdb, category)
        try:
            entries = os.listdir(cat_dir)
        except OSError:
            continue
        for entry in entries:
            split = _split_cpv(entry, category)
            if not split:
                continue
            cp = split[0]
            try:
                with open(os.path.join(cat_dir, entry, "USE"), encoding="utf-8") as fh:
                    flags = set(fh.read().split())
            except OSError:
                continue
            # Union across slots: a flag on in any installed slot counts as on.
            out.setdefault(cp, set()).update(flags)
    return out


def _collect(
    versions: dict[str, tuple[str, dict[str, str], str, dict[str, str]]],
    live_only: dict[str, tuple[str, dict[str, str]]],
    cp: str,
    version: str,
    meta: dict[str, str],
) -> None:
    """Fold one (version, metadata) pair into the per-package best/latest state."""
    if version.split("-")[0] in _LIVE_VERSIONS:
        live_only.setdefault(cp, (version, meta))
        return

    stable = "amd64" in meta.get("KEYWORDS", "").split()
    prev = versions.get(cp)
    if prev is None:
        versions[cp] = (version, meta, version, meta) if stable else ("", {}, version, meta)
        return

    best_v, best_m, latest_v, latest_m = prev
    # Track the newest stable release and the newest release overall
    # separately: the first is what emerge would pull in today, the
    # second is what's available if you accept ~amd64.
    if stable and (not best_v or _vercmp(version, best_v) > 0):
        best_v, best_m = version, meta
    if _vercmp(version, latest_v) > 0:
        latest_v, latest_m = version, meta
    versions[cp] = (best_v, best_m, latest_v, latest_m)


def _scan_md5_cache(repo: Repository, progress: bool) -> tuple[dict, dict]:
    """Fast path: parse the repo's pre-generated metadata cache."""
    versions: dict[str, tuple[str, dict[str, str], str, dict[str, str]]] = {}
    live_only: dict[str, tuple[str, dict[str, str]]] = {}
    try:
        categories = sorted(os.listdir(repo.md5_cache))
    except OSError:
        return versions, live_only

    for n, category in enumerate(categories):
        cat_dir = os.path.join(repo.md5_cache, category)
        if not os.path.isdir(cat_dir):
            continue
        if progress and n % 20 == 0:
            print(f"\r  indexing {repo.name}/{category:<24}", end="", file=sys.stderr, flush=True)
        try:
            entries = os.listdir(cat_dir)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".gz") or entry.startswith("."):
                continue
            split = _split_cpv(entry, category)
            if not split:
                continue
            cp, version = split
            _collect(versions, live_only, cp, version,
                     _parse_metadata(os.path.join(cat_dir, entry)))
    return versions, live_only


def _scan_dbapi(repo: Repository, progress: bool) -> tuple[dict, dict]:
    """Slow path for repos without a metadata cache.

    Most overlays ship ebuilds only, with no ``metadata/md5-cache``, so there
    is nothing to parse directly. Ask Portage for the metadata instead - it
    sources the ebuilds and caches the result itself. Overlays are small
    (a handful of packages), so the cost is negligible.
    """
    versions: dict[str, tuple[str, dict[str, str], str, dict[str, str]]] = {}
    live_only: dict[str, tuple[str, dict[str, str]]] = {}
    keys = ["DESCRIPTION", "IUSE", "KEYWORDS", "SLOT", "REQUIRED_USE"]
    try:
        import portage

        db = portage.db[portage.root]["porttree"].dbapi
        cps = db.cp_all(trees=[repo.location])
    except Exception:
        return versions, live_only

    if progress and cps:
        print(f"\r  indexing {repo.name:<28}", end="", file=sys.stderr, flush=True)

    for cp in cps:
        try:
            cpvs = db.cp_list(cp, mytree=repo.location)
        except Exception:
            continue
        for cpv in cpvs:
            try:
                values = db.aux_get(cpv, keys, myrepo=repo.name)
            except Exception:
                continue
            meta = dict(zip(keys, values))
            version = _split_cpv(cpv.split("/")[-1], cp.split("/")[0])
            if not version:
                continue
            _collect(versions, live_only, cp, version[1], meta)
    return versions, live_only


def build(progress: bool = False) -> list[Package]:
    """Index every configured repository, sorted by package name."""
    installed = _installed_versions()
    # cp -> (best_visible, best_visible_meta, latest_any, latest_meta)
    best: dict[str, tuple[str, dict[str, str], str, dict[str, str]]] = {}
    # Packages that only ever ship a live ebuild still need to be listed.
    live_only: dict[str, tuple[str, dict[str, str]]] = {}
    origin: dict[str, str] = {}

    repos = repositories()
    if not any(r.has_md5_cache for r in repos):
        raise RuntimeError("no readable Portage metadata cache in any repository")

    # Ascending priority: a later repo overrides an earlier one, which is how
    # Portage resolves a package that exists in several repositories.
    for repo in repos:
        if repo.has_md5_cache:
            found, found_live = _scan_md5_cache(repo, progress)
        else:
            found, found_live = _scan_dbapi(repo, progress)
        best.update(found)
        live_only.update(found_live)
        for cp in list(found) + list(found_live):
            origin[cp] = repo.name

    if progress:
        print("\r" + " " * 48 + "\r", end="", file=sys.stderr, flush=True)

    for cp, (version, meta) in live_only.items():
        best.setdefault(cp, ("", {}, version, meta))

    packages = []
    for cp, (best_v, best_m, latest_v, latest_m) in best.items():
        # Fall back to the newest testing version when nothing is stable yet,
        # so the package still shows a usable version (marked ~ on display).
        version, meta = (best_v, best_m) if best_v else (latest_v, latest_m)
        packages.append(
            Package(
                cp=cp,
                version=version,
                installed=installed.get(cp, ""),
                slot=meta.get("SLOT", "0"),
                description=meta.get("DESCRIPTION", ""),
                iuse=meta.get("IUSE", ""),
                keywords=meta.get("KEYWORDS", ""),
                required_use=meta.get("REQUIRED_USE", ""),
                latest=latest_v,
                repo=origin.get(cp, "gentoo"),
            )
        )
    packages.sort(key=lambda p: p.cp)
    return packages


def _sanitize(text: str) -> str:
    """Strip characters that would corrupt a TSV row."""
    return text.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def save(packages: list[Package]) -> str:
    """Write the index atomically so a concurrent reader never sees a partial file."""
    os.makedirs(cache_dir(), exist_ok=True)
    path = index_path()
    fd, tmp = tempfile.mkstemp(dir=cache_dir(), prefix=".packages-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            # Record the repo paths alongside the stamp so is_stale() can
            # watch exactly these without asking Portage what they are.
            locations = [r.location for r in repositories()]
            fh.write(f"#stamp\t{_tree_stamp(locations)}\t{':'.join(locations)}\n")
            for p in packages:
                fh.write(
                    "\t".join(
                        (
                            p.cp,
                            p.version,
                            p.installed,
                            _sanitize(p.slot),
                            _sanitize(p.description),
                            _sanitize(p.iuse),
                            _sanitize(p.keywords),
                            _sanitize(p.required_use),
                            p.latest,
                            _sanitize(p.repo),
                        )
                    )
                    + "\n"
                )
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _parse_row(line: str) -> Package | None:
    fields = line.rstrip("\n").split("\t")
    if len(fields) < 7:
        return None
    return Package(
        cp=fields[0],
        version=fields[1],
        installed=fields[2],
        slot=fields[3],
        description=fields[4],
        iuse=fields[5],
        keywords=fields[6],
        required_use=fields[7] if len(fields) > 7 else "",
        latest=fields[8] if len(fields) > 8 else fields[1],
        repo=fields[9] if len(fields) > 9 else "gentoo",
    )


def is_stale() -> bool:
    """True when the cached index is missing or predates the current tree state."""
    try:
        with open(index_path(), encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return True
    if not first.startswith("#stamp\t"):
        return True
    fields = first.rstrip("\n").split("\t")
    if len(fields) < 2:
        return True
    locations = fields[2].split(":") if len(fields) > 2 and fields[2] else []
    return fields[1] != _tree_stamp(locations)


def load(rebuild_if_stale: bool = True) -> list[Package]:
    """Return the cached index, rebuilding it first if the tree has changed."""
    if rebuild_if_stale and is_stale():
        packages = build()
        try:
            save(packages)
        except OSError:
            pass  # A read-only cache dir shouldn't break lookups.
        return packages

    packages = []
    try:
        with open(index_path(), encoding="utf-8") as fh:
            fh.readline()  # stamp
            for line in fh:
                p = _parse_row(line)
                if p is not None:
                    packages.append(p)
    except OSError:
        return build()
    return packages


def refresh(progress: bool = True) -> tuple[int, str]:
    """Force a rebuild. Returns ``(package_count, index_path)``."""
    packages = build(progress=progress)
    return len(packages), save(packages)


def clear() -> None:
    shutil.rmtree(cache_dir(), ignore_errors=True)
