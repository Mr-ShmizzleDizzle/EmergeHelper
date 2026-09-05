"""Completion queries against the cached package index.

Speed matters here: this runs on every Tab press, so the hot path avoids
parsing rows it is going to discard.  The index file is sorted by ``cp``, and
a cheap ``startswith`` on the raw line filters out almost everything before we
pay for a field split.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import index

# Leading characters of a version-qualified atom, e.g. ">=app-admin/conky-1.2".
_OPERATORS = ("<=", ">=", "!!", "<", ">", "=", "~", "!")

# Portage's built-in package sets, offered when the word starts with "@".
SETS = {
    "@world": "everything you selected, plus system and profile",
    "@selected": "packages and sets you explicitly installed",
    "@selected-packages": "packages listed in /var/lib/portage/world",
    "@selected-sets": "sets listed in /var/lib/portage/world_sets",
    "@system": "packages the profile deems essential",
    "@profile": "packages required by the current profile",
    "@installed": "every installed package",
    "@module-rebuild": "kernel modules needing a rebuild",
    "@preserved-rebuild": "packages linked against preserved libraries",
    "@golang-rebuild": "packages built from Go sources",
    "@live-rebuild": "installed live (9999) ebuilds",
    "@security": "packages with outstanding GLSA security advisories",
    "@changed-deps": "packages whose dependencies changed",
}


@dataclass(slots=True)
class Match:
    """A single completion candidate."""

    text: str  # what gets inserted on the command line
    description: str  # shown beside it in the pager
    installed: bool = False
    rank: int = 0  # lower sorts first


def split_operator(word: str) -> tuple[str, str]:
    """Peel a leading version operator off an atom.

    ``">=app-admin/conky-1.2"`` -> ``(">=", "app-admin/conky-1.2")``
    """
    for op in _OPERATORS:
        if word.startswith(op):
            return op, word[len(op) :]
    return "", word


def _annotate(pkg: index.Package) -> str:
    """Build the description column: summary plus version/installed state."""
    desc = pkg.description or "(no description)"
    version = pkg.version + ("" if pkg.is_stable else " ~")
    if pkg.installed:
        if pkg.installed == pkg.version:
            tag = f"[I {pkg.installed}]"
        else:
            # An upgrade is available - show both, since that is the
            # interesting case when deciding what to emerge.
            tag = f"[I {pkg.installed} -> {version}]"
    else:
        tag = f"[{version}]"
    # Overlay packages are worth calling out: they carry different trust and
    # keywording than the main tree.
    if pkg.is_overlay:
        tag += f" ::{pkg.repo}"
    return f"{desc}  {tag}"


def _iter_rows() -> list[str]:
    """Raw index lines, minus the stamp header. Rebuilds the cache if stale."""
    if index.is_stale():
        try:
            index.save(index.build())
        except (OSError, RuntimeError):
            pass
    try:
        with open(index.index_path(), encoding="utf-8") as fh:
            fh.readline()  # stamp header
            return fh.readlines()
    except OSError:
        return []


def _row_to_package(line: str) -> index.Package | None:
    return index._parse_row(line)


def categories() -> list[str]:
    """Every category present in the index."""
    seen: dict[str, None] = {}
    for line in _iter_rows():
        slash = line.find("/")
        if slash > 0:
            seen[line[:slash]] = None
    return list(seen)


def complete(
    word: str,
    installed_only: bool = False,
    limit: int = 400,
) -> list[Match]:
    """Return completions for a partially typed package atom.

    Handles bare names (``conky``), category prefixes (``app-ad``),
    in-category prefixes (``app-admin/con``), version operators
    (``>=app-admin/conky-1.2``) and package sets (``@world``).
    """
    operator, atom = split_operator(word)

    if atom.startswith("@"):
        return _complete_set(atom)

    rows = _iter_rows()
    if not rows:
        return []

    lowered = atom.lower()
    matches: list[Match] = []

    if "/" in atom:
        matches = _complete_qualified(rows, lowered, operator, installed_only)
    else:
        matches = _complete_bare(rows, lowered, operator, installed_only)

    matches.sort(key=lambda m: (m.rank, not m.installed, m.text))
    return matches[:limit]


def _complete_set(atom: str) -> list[Match]:
    lowered = atom.lower()
    out = [
        Match(text=name, description=desc)
        for name, desc in SETS.items()
        if name.startswith(lowered)
    ]
    out.sort(key=lambda m: m.text)
    return out


def _complete_qualified(
    rows: list[str],
    lowered: str,
    operator: str,
    installed_only: bool,
) -> list[Match]:
    """Complete when the user has already typed a ``/``."""
    category, _, rest = lowered.partition("/")
    out: list[Match] = []

    for line in rows:
        # Cheap reject before paying for a field split.
        if not line.startswith(category):
            continue
        pkg = _row_to_package(line)
        if pkg is None or pkg.category != category:
            continue
        if installed_only and not pkg.is_installed:
            continue

        name = pkg.name.lower()
        if not rest:
            rank = 0
        elif name.startswith(rest):
            rank = 0
        elif rest in name:
            rank = 1  # substring match, ranked below true prefixes
        else:
            continue

        out.append(
            Match(
                text=operator + pkg.cp,
                description=_annotate(pkg),
                installed=pkg.is_installed,
                rank=rank,
            )
        )

    # "app-admin/conky-1.22" - the user has started typing a version, which
    # can't match a bare package name. Offer real versions instead.
    if not out and rest:
        versioned = _complete_versions(category, rest, operator)
        if versioned:
            return versioned

    # Nothing in that category matched - the category itself may be partial,
    # so offer categories instead of leaving the user with an empty pager.
    if not out and rest == "":
        return _complete_categories(rows, category, installed_only)
    return out


def _split_name_version(rest: str) -> tuple[str, str] | None:
    """Split ``conky-1.22`` into ``("conky", "1.22")`` at the version boundary."""
    for i in range(len(rest) - 1, 0, -1):
        if rest[i] == "-" and rest[i + 1 : i + 2].isdigit():
            return rest[:i], rest[i + 1 :]
    return None


def _complete_versions(category: str, rest: str, operator: str) -> list[Match]:
    """Offer concrete versions for a partially typed versioned atom.

    Read straight from the metadata cache directory rather than the index,
    which only records one version per package. It's a single listdir of one
    category, so it stays cheap.
    """
    split = _split_name_version(rest)
    if not split:
        return []
    name, version_prefix = split

    cat_dir = os.path.join(index.MD5_CACHE, category)
    try:
        entries = os.listdir(cat_dir)
    except OSError:
        return []

    installed = index._installed_versions()
    prefix = f"{name}-"
    out: list[Match] = []
    for entry in entries:
        if entry.endswith(".gz") or not entry.startswith(prefix):
            continue
        candidate = entry[len(prefix) :]
        # Guard against "conky-foo-1.2" matching when the user typed "conky".
        if not candidate[:1].isdigit() or not candidate.startswith(version_prefix):
            continue
        cp = f"{category}/{name}"
        is_installed = installed.get(cp) == candidate
        # A bare version needs an operator to be a valid atom; default to "=".
        out.append(
            Match(
                text=f"{operator or '='}{cp}-{candidate}",
                description=(
                    f"version {candidate}" + ("  [installed]" if is_installed else "")
                ),
                installed=is_installed,
                rank=0,
            )
        )
    out.sort(key=lambda m: m.text)
    return out


def _complete_bare(
    rows: list[str],
    lowered: str,
    operator: str,
    installed_only: bool,
) -> list[Match]:
    """Complete a word with no ``/`` yet: match categories and package names."""
    out: list[Match] = []

    # Offer matching categories first so "app-ad<Tab>" narrows to a category.
    # With nothing typed at all, the categories *are* the useful answer -
    # 19k bare package names in a pager helps nobody.
    out.extend(_complete_categories(rows, lowered, installed_only))

    for line in rows:
        # Reject on the cp field alone before splitting the whole row: package
        # names are the only thing we match here, and most rows won't contain
        # the typed text at all.
        tab = line.find("\t")
        if lowered and lowered not in line[:tab].lower():
            continue
        pkg = _row_to_package(line)
        if pkg is None:
            continue
        if installed_only and not pkg.is_installed:
            continue
        name = pkg.name.lower()
        if not lowered:
            # Bare Tab with nothing typed: only suggest installed packages,
            # since 19k unfiltered entries are useless in a pager.
            if not pkg.is_installed:
                continue
            rank = 2
        elif name == lowered:
            rank = 0
        elif name.startswith(lowered):
            rank = 1
        elif lowered in name:
            rank = 3
        else:
            continue
        out.append(
            Match(
                text=operator + pkg.cp,
                description=_annotate(pkg),
                installed=pkg.is_installed,
                rank=rank,
            )
        )
    return out


def _complete_categories(
    rows: list[str],
    prefix: str,
    installed_only: bool = False,
) -> list[Match]:
    counts: dict[str, int] = {}
    for line in rows:
        slash = line.find("/")
        if slash <= 0:
            continue
        category = line[:slash]
        if not category.startswith(prefix):
            continue
        if installed_only:
            # Field 2 is the installed version; empty means not installed.
            parts = line.split("\t", 3)
            if len(parts) < 3 or not parts[2]:
                continue
        counts[category] = counts.get(category, 0) + 1
    noun = "installed" if installed_only else "packages"
    return [
        # Trailing slash so the next Tab drills into the category.
        Match(text=f"{c}/", description=f"category, {n} {noun}", rank=0)
        for c, n in sorted(counts.items())
    ]


def lookup(cp: str) -> index.Package | None:
    """Fetch a single package by exact ``category/name``."""
    prefix = cp + "\t"
    for line in _iter_rows():
        if line.startswith(prefix):
            return _row_to_package(line)
    return None


def format_for_fish(matches: list[Match]) -> str:
    """Render matches as fish expects: ``value<TAB>description`` per line."""
    return "".join(f"{m.text}\t{m.description}\n" for m in matches)


def main(argv: list[str]) -> int:
    """CLI shim used by the shell completion hooks."""
    installed_only = False
    args = []
    for a in argv:
        if a == "--installed":
            installed_only = True
        elif a == "--":
            continue  # separator from the shell hook; the word may start with "-"
        else:
            args.append(a)
    word = args[0] if args else ""
    try:
        matches = complete(word, installed_only=installed_only)
    except Exception:
        # A broken cache must never break the user's shell completion.
        return 0
    os.write(1, format_for_fish(matches).encode("utf-8", "replace"))
    return 0
