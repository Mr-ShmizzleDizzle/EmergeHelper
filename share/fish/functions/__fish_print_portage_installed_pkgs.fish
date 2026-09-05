# Installed-package lister used by emerge -C / --depclean / --config completions.
# Same idea as the available-packages override: answer from the cached index,
# filtered by what's already typed.
function __fish_print_portage_installed_pkgs --description 'Print installed packages (EmergeHelper)'
    if command -q eh
        eh complete --installed -- (commandline -ct) 2>/dev/null
        return
    end

    set -l vdb /var/db/pkg
    test -d $vdb; or return
    find $vdb -mindepth 2 -maxdepth 2 -type d -printf '%P\n' 2>/dev/null |
        string replace -r -- '-[0-9][^-]*(-r[0-9]+)?$' ''
end
