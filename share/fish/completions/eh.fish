# Completions for EmergeHelper's own `eh` command.

function __eh_packages
    eh complete -- (commandline -ct) 2>/dev/null
end

function __eh_installed
    eh complete --installed -- (commandline -ct) 2>/dev/null
end

function __eh_no_subcommand
    not __fish_seen_subcommand_from install i update u search s info index changes complete
end

# Action flags: `eh -i emacs` is the same as `eh install emacs`.
complete -c eh -n __eh_no_subcommand -s i -l install -d 'Merge packages'
complete -c eh -n __eh_no_subcommand -s u -l update -d 'Update @world'
complete -c eh -n __eh_no_subcommand -s s -l search -d 'Search packages'
complete -c eh -n __eh_no_subcommand -s Q -l info -d 'Show a package and its USE flags'
complete -c eh -n __eh_no_subcommand -s x -l index -d 'Inspect or rebuild the package index'
complete -c eh -n __eh_no_subcommand -s L -l changes -d 'Show config eh has written'

# Package arguments for the flag forms.
complete -c eh -n '__fish_seen_argument -s i -l install -s u -l update -s Q -l info' \
    -xa '(__eh_packages)'

# Subcommands (only before one has been chosen).
complete -c eh -n __eh_no_subcommand -a install -d 'Merge packages, prompting for any config changes'
complete -c eh -n __eh_no_subcommand -a i -d 'Alias for install'
complete -c eh -n __eh_no_subcommand -a update -d 'Update @world'
complete -c eh -n __eh_no_subcommand -a u -d 'Alias for update'
complete -c eh -n __eh_no_subcommand -a search -d 'Search packages'
complete -c eh -n __eh_no_subcommand -a s -d 'Alias for search'
complete -c eh -n __eh_no_subcommand -a info -d 'Show a package and its USE flags'
complete -c eh -n __eh_no_subcommand -a index -d 'Inspect or rebuild the package index'
complete -c eh -n __eh_no_subcommand -a changes -d 'Show config eh has written'

# Package arguments, pulled live from the index.
complete -c eh -n '__fish_seen_subcommand_from install i' -xa '(__eh_packages)'
complete -c eh -n '__fish_seen_subcommand_from update u' -xa '(__eh_packages)'
complete -c eh -n '__fish_seen_subcommand_from info search s' -xa '(__eh_packages)'

# Merge options.
for sub in install i update u
    complete -c eh -n "__fish_seen_subcommand_from $sub" -s p -l pretend \
        -d 'Resolve and show changes, merge nothing'
    complete -c eh -n "__fish_seen_subcommand_from $sub" -s y -l yes \
        -d 'Accept the recommended scope for every change'
    complete -c eh -n "__fish_seen_subcommand_from $sub" -l plain \
        -d 'Run emerge directly, without the progress UI'
    complete -c eh -n "__fish_seen_subcommand_from $sub" -l no-binpkg \
        -d 'Ignore binary packages when resolving'
    complete -c eh -n "__fish_seen_subcommand_from $sub" -s o -l emerge-opt -r \
        -d 'Extra option passed through to emerge'
end

complete -c eh -n '__fish_seen_subcommand_from search s' -s I -l installed -d 'Only installed packages'
complete -c eh -n '__fish_seen_subcommand_from search s' -s n -l limit -r -d 'Maximum results'
complete -c eh -n '__fish_seen_subcommand_from index' -l refresh -d 'Force a rebuild'
