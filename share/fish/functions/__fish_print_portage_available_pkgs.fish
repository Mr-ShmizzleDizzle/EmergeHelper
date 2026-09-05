# Overrides fish's built-in package lister, which shells out to `find` across
# the whole ebuild tree on every Tab (~1.1s, 19k bare names, no descriptions).
#
# EmergeHelper answers from a cached index instead, and filters against the
# word being typed so the pager narrows as you type rather than dumping the
# entire tree. Falls back to the original behaviour if `eh` isn't on PATH.
function __fish_print_portage_available_pkgs --description 'Print available packages (EmergeHelper)'
    if command -q eh
        eh complete -- (commandline -ct) 2>/dev/null
        return
    end

    set -l paths (__fish_print_portage_repository_paths)
    set -q paths[1]
    or return
    find $paths -mindepth 2 -maxdepth 2 -type d \
        ! '(' '(' -path '*/eclass/*' -o -path '*/metadata/*' -o -path '*/profiles/*' \
        -o -path '*/.*/*' ')' -prune ')' -printf '%P\n' 2>/dev/null
end
