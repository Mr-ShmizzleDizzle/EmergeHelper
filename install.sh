#!/bin/bash
# Install EmergeHelper for the current user.
#
# Everything is symlinked rather than copied, so editing the checkout takes
# effect immediately. Nothing here needs root: eh escalates with sudo only
# when it actually merges packages or writes /etc/portage.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
FISH_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish"

info() { printf '  %s\n' "$*"; }

echo "Installing EmergeHelper from ${ROOT}"

mkdir -p "$BIN_DIR" "$FISH_DIR/functions" "$FISH_DIR/completions"

# --- clean up the previous name (PortageHelper / ph) ----------------------
for stale in "$BIN_DIR/ph" "$FISH_DIR/completions/ph.fish"; do
    if [ -L "$stale" ]; then
        rm -f "$stale"
        info "removed old $stale"
    fi
done

# Config files keep working under either name (Portage reads the whole
# directory), but leaving both would double every entry once we start writing
# the new one, so rename rather than copy.
legacy_found=""
for d in package.use package.accept_keywords package.license; do
    [ -f "/etc/portage/$d/zz-portagehelper" ] && legacy_found="yes"
done
if [ -n "$legacy_found" ]; then
    echo
    echo "Found config written under the old name (zz-portagehelper)."
    printf 'Rename it to zz-emergehelper? [Y/n] '
    read -r reply
    case "$reply" in
        [Nn]*) info "left as-is; eh still reads the old files" ;;
        *)
            for d in package.use package.accept_keywords package.license; do
                old="/etc/portage/$d/zz-portagehelper"
                [ -f "$old" ] || continue
                if sudo mv -n "$old" "/etc/portage/$d/zz-emergehelper"; then
                    info "renamed $d/zz-portagehelper -> zz-emergehelper"
                else
                    info "could not rename $old (left in place)"
                fi
            done
            ;;
    esac
    echo
fi

chmod +x "$ROOT/bin/eh"
ln -sfn "$ROOT/bin/eh" "$BIN_DIR/eh"
info "eh            -> $BIN_DIR/eh"

for f in "$ROOT"/share/fish/functions/*.fish; do
    ln -sfn "$f" "$FISH_DIR/functions/$(basename "$f")"
    info "$(basename "$f" .fish) -> $FISH_DIR/functions/"
done

for f in "$ROOT"/share/fish/completions/*.fish; do
    ln -sfn "$f" "$FISH_DIR/completions/$(basename "$f")"
    info "$(basename "$f") -> $FISH_DIR/completions/"
done

echo
echo "Building the package index (one-off, a couple of seconds)…"
"$ROOT/bin/eh" index --refresh

echo
if ! printf '%s' "$PATH" | grep -q "$BIN_DIR"; then
    echo "NOTE: $BIN_DIR is not on your PATH; add it to use 'eh' directly."
fi

cat <<'EOF'

Done. In a new fish shell:

  emerge -av app-admin/<Tab>    fast completions with descriptions
  eh install app-admin/conky    merge with the progress UI
  eh search conky               search the index
  eh info app-admin/conky       package details and USE flags
  eh changes                    what eh has written to /etc/portage

While a build runs, Ctrl+O toggles between the progress meter and the
raw build output.
EOF
