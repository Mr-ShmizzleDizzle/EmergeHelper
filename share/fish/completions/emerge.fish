# emerge completions, accelerated by EmergeHelper.
#
# Replaces fish's stock emerge.fish, which spends ~1.1s walking the ebuild
# tree with `find` and another ~0.45s running `emerge --list-sets` on every
# single Tab press. Both are answered here from EmergeHelper's cached index
# and a static set list instead, and results carry descriptions, versions and
# installed markers.
#
# This file shadows /usr/share/fish/completions/emerge.fish because
# ~/.config/fish/completions comes first in $fish_complete_path.

function __emergehelper_sets --description 'Portage package sets'
    printf '%s\t%s\n' \
        @world 'selected, system and profile combined' \
        @selected 'packages and sets you explicitly installed' \
        @selected-packages 'packages listed in /var/lib/portage/world' \
        @selected-sets 'sets listed in /var/lib/portage/world_sets' \
        @system 'packages the profile deems essential' \
        @profile 'packages required by the current profile' \
        @installed 'every installed package' \
        @security 'packages with outstanding GLSA advisories' \
        @module-rebuild 'kernel modules needing a rebuild' \
        @preserved-rebuild 'packages linked against preserved libraries' \
        @live-rebuild 'installed live (9999) ebuilds' \
        @golang-rebuild 'packages built from Go sources' \
        @changed-deps 'packages whose dependencies changed' \
        @changed-subslot 'packages whose subslot dependencies changed' \
        @downgrade 'installed packages newer than the tree' \
        @unavailable 'installed packages no longer in any repo' \
        @rebuilt-binaries 'binary packages rebuilt since installation'
end

# Package arguments. `eh` filters against the word being typed, so the pager
# narrows as you type instead of listing the whole tree every time.
function __emergehelper_args
    set -l token (commandline -ct)

    # Only sets are meaningful once the word starts with "@".
    if string match -qr '^@' -- $token
        __emergehelper_sets
        return
    end

    # Actions that operate on what's already installed.
    if __fish_contains_opt config -s c depclean deselect info -s P prune -s C unmerge
        eh complete --installed -- $token 2>/dev/null
        __emergehelper_sets
        return
    end

    # Nothing sensible to complete for these.
    if __fish_contains_opt check-news -s h help list-sets metadata regen -s r resume \
            -s s search -s S searchdesc sync -s V version
        return
    end

    eh complete -- $token 2>/dev/null
    __emergehelper_sets
end

complete -c emerge -xa '(__emergehelper_args)'

#########################
# Actions and options   #
#########################
complete -c emerge -l check-news
complete -c emerge -l clean
complete -c emerge -l config
complete -c emerge -s c -l depclean
complete -c emerge -l deselect
complete -c emerge -s h -l help
complete -c emerge -l info
complete -c emerge -l list-sets
complete -c emerge -l metadata
complete -c emerge -s P -l prune
complete -c emerge -l regen
complete -c emerge -s r -l resume
complete -c emerge -s s -l search
complete -c emerge -s S -l searchdesc
complete -c emerge -l sync
complete -c emerge -s C -l unmerge
complete -c emerge -s V -l version
complete -c emerge -s A -l alert -d "Add a terminal bell character ('\a') to all interactive prompts"
complete -c emerge -s a -l ask -d "Prompt the user before performing the merge"
complete -c emerge -l backtrack
complete -c emerge -s b -l buildpkg -d "Build a binary pkg additionally"
complete -c emerge -s B -l buildpkgonly -d "Only build a binary pkg"
complete -c emerge -s U -l changed-use
complete -c emerge -s l -l changelog -d "Show changelog of pkg. Use with --pretend"
complete -c emerge -l color -d "Colorized output" \
    -xa "y n"
complete -c emerge -l columns -d "Align output. Use with --pretend"
complete -c emerge -l complete-graph
complete -c emerge -s d -l debug -d "Run in debug mode"
complete -c emerge -s D -l deep -d "Consider the whole dependency tree"
complete -c emerge -s e -l emptytree -d "Reinstall all world pkgs"
complete -c emerge -l exclude
complete -c emerge -s f -l fetchonly -d "Only download the pkgs but don't install them"
complete -c emerge -s F -l fetch-all-uri -d "Same as --fetchonly and grab all potential files"
complete -c emerge -s g -l getbinpkg -d "Download infos from each binary pkg. Implies -k"
complete -c emerge -s G -l getbinpkgonly -d "As -g but don't use local infos"
complete -c emerge -l ignore-default-opts -d "Ignore EMERGE_DEFAULT_OPTS"
complete -c emerge -l jobs
complete -c emerge -l keep-going
complete -c emerge -s N -l newuse -d "Include installed pkgs with changed USE flags"
complete -c emerge -l noconfmem -d "Disregard merge records"
complete -c emerge -s O -l nodeps -d "Don't merge dependencies"
complete -c emerge -s n -l noreplace -d "Skip already installed pkgs"
complete -c emerge -l nospinner -d "Disable the spinner"
complete -c emerge -s 1 -l oneshot -d "Don't add pkgs to world"
complete -c emerge -s o -l onlydeps -d "Only merge dependencies"
complete -c emerge -s p -l pretend -d "Display what would be done without doing it"
complete -c emerge -s q -l quiet -d "Use a condensed output"
complete -c emerge -s w -l select
complete -c emerge -l skipfirst -d "Remove first pkg in resume list. Use with --resume"
complete -c emerge -s t -l tree -d "Show the dependency tree"
complete -c emerge -s u -l update
complete -c emerge -s k -l usepkg -d "Use binary pkg if available"
complete -c emerge -s K -l usepkgonly -d "Only use binary pkgs"
complete -c emerge -s v -l verbose -d "Run in verbose mode"
complete -c emerge -l verbose-conflicts -d "Verbose slot conflicts"
complete -c emerge -l with-bdeps -d "Pull in build time dependencies" \
    -xa "y n"

#########################
# Additions             #
#########################
# Options the stock file left commented out but that matter day to day.
complete -c emerge -s a -l ask -d "Prompt before merging"
complete -c emerge -l autounmask -xa "y n" -d "Automatically resolve masked/USE problems"
complete -c emerge -l autounmask-write -xa "y n" -d "Write autounmask changes to /etc/portage"
complete -c emerge -l autounmask-continue -xa "y n" -d "Apply changes and continue merging"
complete -c emerge -l autounmask-only -xa "y n" -d "Only report changes, merge nothing"
complete -c emerge -l autounmask-keep-keywords -xa "y n" -d "Don't propose keyword changes"
complete -c emerge -l autounmask-keep-masks -xa "y n" -d "Don't propose mask changes"
complete -c emerge -l binpkg-respect-use -xa "y n" -d "Ignore binpkgs with mismatched USE"
complete -c emerge -l quiet-build -xa "y n" -d "Redirect build output to logs"
complete -c emerge -l quiet-fail -xa "y n" -d "Suppress build output on failure"
complete -c emerge -l read-news -xa "y n" -d "Offer to read unread news"
complete -c emerge -l rebuild-if-new-slot -xa "y n" -d "Rebuild when a dependency gains a slot"
complete -c emerge -l reinstall -xa changed-use -d "Reinstall on USE changes"
complete -c emerge -l root-deps -xa "rdeps" -d "Install dependencies into ROOT"
complete -c emerge -l with-test-deps -d "Pull in test dependencies"
complete -c emerge -l load-average -x -d "Pause when load exceeds this"
complete -c emerge -l exclude -xa '(__emergehelper_args)' -d "Skip these packages"
complete -c emerge -l usepkg-exclude -xa '(__emergehelper_args)' -d "Never use binpkgs for these"
complete -c emerge -l rebuild-exclude -xa '(__emergehelper_args)' -d "Never rebuild these"
complete -c emerge -l reinstall-atoms -xa '(__emergehelper_args)' -d "Treat these as needing reinstall"
