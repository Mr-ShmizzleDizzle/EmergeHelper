# EmergeHelper

A friendlier front-end for Portage on Gentoo.

Anyone who doesn't want to use a terminal isn't going to use Gentoo, so a MacOS-grade 
"push this button here and everything's done for you" isn't what I'm going for; my goal
is *convenience*. EmergeHelper (eh) is a convenient way to use Portage if you have a life
and don't want to spend needless amounts of time setting individual USE flags for every
single package, don't like getting dizzy from watching everything compile, 
and also don't want to type "emerge -s [package_name]" and scroll up for miles. Specifics below:


1. **A progress meter for builds**, with `Ctrl+O` to switch to the raw
   compiler output (if you miss the vertigo-maker 3000) and back.
2. **USE-flag problems become a prompt instead of die** — with the real blast
   radius of each choice, so you can see when a system-wide setting would
   break something.
3. **Fast package-name completion** that narrows as you type, pulled straight
   from Portage's metadata.

Dependencies: How about none? No new packages to emerge: it runs on Python 3 with `psutil` (already
installed) and stdlib `curses`.

## Install

```
git clone https://github.com/Mr-ShmizzleDizzle/EmergeHelper.git
cd EmergeHelper
./install.sh
```

Symlinks `eh` into `~/.local/bin`, installs the fish completions, and builds
the package index. Nothing needs root — `eh` escalates with `sudo` only when
it actually merges packages or writes to `/etc/portage`.

## The progress meter

```sh
eh -i app-admin/conky         # or: eh install app-admin/conky
eh -i conky htop mpv          # several at once, resolved as one merge
eh -u                         # @world --update --deep --newuse
```

Every action has a short flag and a word form — use whichever is faster:

| Flag | Meaning | Does |
| --- | ----------- | --- |
| `-i` `--install` | `install` | Merge packages |
| `-u` `--update` | `update` | Update `@world` |
| `-s` `--search` | `search` | Search the index |
| `-Q` `--info` | `info` | Package details and USE flags |
| `-x` `--index` | `index` | Inspect or rebuild the index |
| `-L` `--changes` | `changes` | Show config `eh` has written |
| `-T` `--tidy` | `tidy` | Repair the managed `package.use` file |

The action flag can go anywhere, so `eh -i emacs -y` works. Combining it with
other short flags (`-iy`) does not.

Name as many packages as you like, in any arrangement — `eh -i vlc -y mpv`
and `eh -i vlc -i mpv` both merge both packages. They are resolved together as
a single request, so a USE flag one of them needs is only asked about once.

While it runs:

```
 EmergeHelper — emerge app-admin/conky                              running
 ────────────────────────────────────────────────────────────────────────────
 Overall ███████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░   42.1%
         5/12 jobs   elapsed 4:07   eta 5:41        3 running

 Building media-libs/libvpx-1.16.0   1:12
   ✓ fetch → ✓ unpack → ✓ prepare → ✓ configure → ● compile → ○ test → ○ install

 Resources
   build cpu     782%  (65.2% of 12 cores)
   build ram     2.1G  peak 3.4G
   load avg      9.44  8.10  6.02
   sys ram       6.8G / 15.0G  (45%)
   build tmp   458.6G free
```

| Key | Action |
| --- | --- |
| `Ctrl+O` | Toggle between the meter and the raw build output |
| `↑` `↓` `PgUp` `PgDn` | Scroll the raw output |
| `End` | Jump back to following the tail |
| `Ctrl+C` | Interrupt the build |
| `q` | Quit once the build has finished |

Every run is also written in full to `~/.cache/emergehelper/logs/`, ANSI
colour intact, so `less -R` on it afterwards works. The sudo exchange at the
start is deliberately **not** logged: a PTY can echo what is typed at a
password prompt, so logging begins at Portage's first output.

Run `eh` as yourself, not under `sudo` — it escalates on its own. (`sudo eh`
usually fails anyway, since sudo's `secure_path` doesn't include
`~/.local/bin`.)

You are asked for your password by sudo, just as the merge starts, on your
real terminal — `eh` bridges it to the PTY until Portage's first output
appears, then hands the screen to the UI. Three things make that work, all
learned the hard way:

- emerge runs on a PTY that `eh` gives its own session **and** sets as the
  controlling terminal. With only `setsid()`, sudo refuses to prompt at all —
  *"a terminal is required to read the password"*.
- `eh` never pre-authenticates with `sudo -v`. Run as a nested subprocess sudo
  silently declines to render its prompt while still consuming all three
  attempts, so you get *"Sorry, try again"* with nothing to type into. sudo
  only behaves as the direct child of the PTY.
- `eh` never *predicts* whether a prompt is coming either. sudo's default
  `timestamp_type` is `tty`, so a live timestamp belongs to the terminal it
  was authenticated on — not to the PTY just opened for emerge, where sudo
  asks again. `sudo -n true` succeeding proves nothing about the PTY, and
  skipping the bridge on that basis leaves the prompt stranded behind the
  full-screen UI with no way to answer it.

That last point is also why each merge asks again even when you authenticated
a moment ago: the ticket belongs to the terminal, and every merge gets a fresh
PTY. On a system configured with `timestamp_type=global` (or `!tty_tickets`) a
live ticket does cover it, sudo stays quiet, and the bridge hands over to the
UI the moment Portage speaks — no configuration on `eh`'s side either way.

Phase detection follows the markers Portage itself emits (`>>> Compiling
source in …`, `>>> Jobs: 2 of 15 complete, 3 running`), and CPU/memory are
sampled across the whole build process tree, not just the emerge process.

## USE flags: a prompt instead of a failure

A normal `emerge` stops when a dependency needs a USE flag you don't have set.
`eh` resolves the merge first, in a **throwaway copy of `/etc/portage`**, and
turns each required change into a choice:

```
USE flag change 1 of 1  ─ media-libs/libvpx-1.16.0
  +postproc  Enable additional post processing filters
  required by www-client/firefox-140.15.0
  this flag exists in 2 packages (1 of them installed)

  1) =media-libs/libvpx-1.16.0 postproc    this version only
  2) media-libs/libvpx postproc            this package, all versions  [recommended]
  3) */* postproc                          every package on the system
     would change 23 installed package(s)
     ! 2 would break REQUIRED_USE:
       dev-libs/libusb
       sys-apps/pciutils

  choose (number, s=skip, d=details, a=abort) [2]
```

The point of option 3's warning: setting a flag globally (the `make.conf`
route) can silently break unrelated packages. `eh` checks every installed
package against its `REQUIRED_USE` using **Portage's own evaluator**, so a
reported conflict is a real constraint violation, not a guess. That is why the
narrow, per-package scope is the default recommendation.

When a flag can't satisfy `REQUIRED_USE` on its own, `eh` works out what else
is needed and offers it as one option:

```
  ! this flag alone won't satisfy the package's REQUIRED_USE
    also enabling: bundled-toluapp would satisfy it

  c) app-admin/conky lua-cairo bundled-toluapp   this package, with the flags it requires
```

Keyword (`~amd64`) and license acceptances are prompted the same way, including
the case where emerge would otherwise just say *"All ebuilds that could satisfy
… have been masked"* and stop:

```
Keyword change 1 of 1  ─ =games-util/steam-launcher-1.0.0.87
  ~amd64  this version is not marked stable on your arch
  required by games-util/steam-launcher
```

A `**` keyword (the package has **no** keyword for your arch at all) is called
out separately, since it's a much bigger claim than `~amd64`.

Packages held back by `package.mask` are deliberately **not** offered for
unmasking — a mask entry means a maintainer marked the package broken, unsafe,
or on its way out. `eh` reports the mask reason and stops there.

Portage only reports the changes blocking the *current* resolve, so satisfying
one batch can uncover the next — a 32-bit multilib stack surfaces a handful of
packages at a time. `eh` keeps resolving and prompting until the plan comes
back clean, rather than making you re-run it per round. When you can see a
change is going to repeat across many packages (`abi_x86_32` for Steam, say),
the global option shows you exactly how many installed packages it would touch,
so you can settle it in one decision.

**Nothing is written until you pick.** Everything `eh` writes goes to files it
owns, each line annotated with what required it:

- `/etc/portage/package.use/zz-emergehelper`
- `/etc/portage/package.accept_keywords/zz-emergehelper`
- `/etc/portage/package.license/zz-emergehelper`

Review them any time with `eh changes`. They're plain files — edit or delete
them by hand. The `zz-` prefix makes them sort last, so they win over your
hand-written files.

Use `-p` to see what would be written without writing it, or `-y` to accept
the recommended scope for everything.

### When one flag keeps coming back

A 32-bit multilib stack (Steam) needs `abi_x86_32` on dozens of packages, and
Portage only reveals a few per resolve. Once `eh` has written the same flag for
three or more packages it says so and moves the recommendation to global scope:

```
  ! you've already set abi_x86_32 on 6 packages one at a time
    Portage reveals these a few at a time, so this can repeat for
    a while. Setting it globally settles the whole class at once.

  3) */* abi_x86_32   every package on the system  [recommended]
     would change 213 installed package(s)
```

The count is still shown, because a global flag is a real cost — you're just
no longer asked to rediscover that one package at a time.

**Specificity, not file order.** Portage ranks `package.use` entries by how
specific the atom is, so `media-libs/mesa -abi_x86_32` beats `*/* abi_x86_32`
*wherever* it appears in the file. A leftover per-package line will therefore
silently neuter a global one, and Portage will keep asking for the same change
forever. When `eh` writes a `*/*` entry it now clears that flag from the more
specific lines it is meant to supersede, and reports what it dropped.

`eh -T` repairs a file that is already in that state (and folds in any
pre-rename `zz-portagehelper` file); `eh -T -p` shows what it would do first.

Three guards back this up: if Portage asks for the opposite of a flag it
requested earlier in the same run, `eh` stops rather than flip-flopping the
file; if a change reverses something `eh` wrote in an earlier run, it says so
before replacing the line; and if a resolve comes back asking for *exactly*
what was just written, `eh` stops and tells you what to grep for rather than
looping.

Changes Portage suggests only to break a circular dependency are labelled as
such — they're temporary and can be reverted once the package is installed.

## Completion

`emerge -av app-admin/<Tab>` now lists the packages in that category with
descriptions, versions and installed markers, filtering as you keep typing:

```
app-admin/conky      An advanced, highly configurable system monitor for X  [1.22.3]
app-admin/sudo       Allows users or groups to run commands as other users  [I 1.9.17_p2]
app-admin/eselect    Gentoo's multi-purpose configuration and management tool  [I 1.4.31]
```

`[I …]` means installed; `[I 1.2 -> 1.3]` means an upgrade is available; a
trailing `~` means the version needs keywording.

**Overlays are indexed too**, and tagged so you can tell where a package comes
from:

## Other commands

```sh
eh -s conky                  # search the index
eh -Q app-admin/conky        # description, versions, USE flags with meanings
eh -L                        # what eh has written to /etc/portage
eh -x --refresh              # rebuild the package index
```

To pass an option straight through to emerge, use `--emerge-opt=`. The `=` is
required for options starting with `-`:

```sh
eh install app-admin/conky --emerge-opt=--oneshot
```

## Layout

| Path | Role |
| --- | --- |
| `emergehelper/index.py` | Package index built from Portage's `md5-cache` |
| `emergehelper/complete.py` | Completion queries against the index |
| `emergehelper/preflight.py` | Dry-run resolve in a sandboxed `PORTAGE_CONFIGROOT` |
| `emergehelper/scope.py` | Blast-radius analysis and `REQUIRED_USE` checking |
| `emergehelper/prompts.py` | The interactive prompts; the only code that writes config |
| `emergehelper/monitor.py` | Runs emerge under a PTY, parses phases, samples resources |
| `emergehelper/ui.py` | The curses progress meter and raw-output view |
| `emergehelper/cli.py` | The `eh` command |
| `share/fish/` | fish completions and function overrides |

Uninstall by deleting the symlinks that `install.sh` created in
`~/.local/bin` and `~/.config/fish/{functions,completions}`, plus
`~/.cache/emergehelper`.
