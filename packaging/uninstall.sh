#!/bin/sh
# Strata Terminal: uninstaller for Linux (x86_64, and ARM64 on the NVIDIA DGX Spark) and macOS (Apple silicon).
#
#     curl -fsSL --connect-timeout 15 --max-time 120 https://github.com/Niko1221/Strata/releases/latest/download/uninstall.sh -o uninstall.sh && sh uninstall.sh
#
# What it removes, in order (and nothing else):
#   1. stops a Strata server that is still running (`strata launch --stop`), so no file is in use;
#   2. removes the `strata` link in ~/.local/bin, and only when it points into Strata's own folder;
#   3. removes ${XDG_DATA_HOME:-~/.local/share}/strata/terminal: the installed copies and the `current` link.
#
# It keeps your data. A model folder is never touched, and Strata's engine, settings and chats are kept unless --purge is given.
#
# Options:
#   --plan    show what would be removed, change nothing
#   --purge   also remove Strata's engine components, saved state, settings and chats (the folders are printed first)
#   --yes     ask nothing (a script has no one to ask; --purge without --yes stops when nothing can answer)
#   -h, --help  this text
#
# Settings, the same names install.sh reads (all optional): STRATA_DATA_DIR (default ~/.local/share/strata),
# STRATA_BIN_DIR (default ~/.local/bin), STRATA_HOME (default ~/.strata), XDG_STATE_HOME, XDG_CONFIG_HOME.
set -eu

DATA_DIR="${STRATA_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/strata}"
BIN_DIR="${STRATA_BIN_DIR:-$HOME/.local/bin}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/strata"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/strata"
HOME_DIR="${STRATA_HOME:-$HOME/.strata}"
trim() { case "$1" in /|'') printf '%s' "$1" ;; *) printf '%s' "${1%/}" ;; esac; }
DATA_DIR=$(trim "$DATA_DIR")
BIN_DIR=$(trim "$BIN_DIR")
STATE_DIR=$(trim "$STATE_DIR")
CONFIG_DIR=$(trim "$CONFIG_DIR")
HOME_DIR=$(trim "$HOME_DIR")

say() { printf '%s\n' "$*"; }
die() { printf 'strata uninstaller: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Strata Terminal uninstaller (Linux x86_64, Linux ARM64, macOS Apple silicon)

  sh uninstall.sh [--plan] [--purge] [--yes]

  --plan      show what would be removed, change nothing
  --purge     also remove Strata's engine components, saved state, settings and chats
  --yes       ask nothing (needed for --purge where nothing can answer)
  -h, --help  this text

Settings (optional): STRATA_DATA_DIR, STRATA_BIN_DIR, STRATA_HOME, XDG_STATE_HOME, XDG_CONFIG_HOME.
EOF
}

plan=0; purge=0; yes=0
for arg in "$@"; do
  case "$arg" in
    --plan) plan=1 ;;
    --purge) purge=1 ;;
    --yes|-y) yes=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'strata uninstaller: unknown option: %s (see --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

terminal="$DATA_DIR/terminal"
current="$terminal/current"
link="$BIN_DIR/strata"

# The `strata` command is ours only when the link points into Strata's own folder: another `strata` (a source checkout's,
# for example) is left alone.
link_ours=0
if [ -L "$link" ]; then
  target=$(readlink "$link" 2>/dev/null || true)
  case "$target" in
    "$terminal"/*) link_ours=1 ;;
  esac
fi

# The installed copies: every folder under terminal/ that holds bin/strata. `current` is the link to one of them, not a copy.
copies=0
if [ -d "$terminal" ] && [ ! -L "$terminal" ]; then
  for d in "$terminal"/*; do
    [ -e "$d" ] || continue
    [ "$d" = "$current" ] && continue
    if [ -f "$d/bin/strata" ]; then copies=$((copies + 1)); fi
  done
fi
current_is_link=0
if [ -L "$current" ]; then current_is_link=1; fi

say 'Strata Terminal uninstaller'
say "  Strata files: $terminal"
say "  The strata command: $link"

installed=0
if [ "$copies" != 0 ] || [ "$current_is_link" = 1 ] || [ "$link_ours" = 1 ]; then installed=1; fi
if [ "$installed" = 0 ] && [ "$purge" = 0 ]; then
  say 'Strata is not installed for this user. Nothing was changed.'
  if [ -z "${STRATA_DATA_DIR:-}" ]; then
    say "  (the folder above is the default; set STRATA_DATA_DIR if Strata is somewhere else)"
  fi
  say 'Add --purge to remove Strata data that is left over.'
  exit 0
fi

# ---- what --purge would remove: Strata's own folders, never a model folder
purge_list() {
  for base in "$DATA_DIR" "$STATE_DIR"; do
    for sub in components runtime tuning time-skip draft-costs; do
      if [ -e "$base/$sub" ]; then printf '%s\n' "$base/$sub"; fi
    done
  done
  if [ -z "${STRATA_HOME:-}" ] && [ -e "$HOME_DIR" ]; then printf '%s\n' "$HOME_DIR"; fi
  if [ -e "$CONFIG_DIR" ]; then printf '%s\n' "$CONFIG_DIR"; fi
}

if [ "$purge" = 1 ]; then
  if [ -n "${STRATA_HOME:-}" ]; then
    say "  kept $HOME_DIR (STRATA_HOME is set; it is your own folder)"
  fi
  purge_names=$(purge_list)
  if [ -n "$purge_names" ]; then
    say 'These Strata folders would be removed as well:'
    printf '%s\n' "$purge_names" | sed 's/^/  /'
  else
    say 'There is no Strata data to remove.'
  fi
  if [ -n "$purge_names" ] && [ "$plan" = 0 ] && [ "$yes" = 0 ]; then
    if [ ! -t 0 ]; then
      die 'nothing can answer here, so --purge needs --yes. Add --yes to remove the data, or leave --purge out to keep it.'
    fi
    printf 'Remove them? Model folders are not touched. [y/N] '
    reply=''
    IFS= read -r reply || reply=''
    case "$reply" in
      y|Y|yes|YES|Yes) ;;
      *) purge=0; say 'Your data will be kept.' ;;
    esac
  fi
fi

# ---- 1. a running server first: its engine keeps files open
if [ "$plan" = 1 ]; then
  say '  a running Strata server would be stopped (strata launch --stop)'
else
  runner=''
  if [ "$link_ours" = 1 ] && [ -x "$link" ]; then runner="$link"
  elif [ "$installed" = 1 ] && [ -x "$current/bin/strata" ]; then runner="$current/bin/strata"
  fi
  if [ -n "$runner" ]; then
    say '  asking a running Strata server to stop (strata launch --stop)'
    "$runner" launch --stop >/dev/null 2>&1 || true
  fi
fi

# ---- 2 and 3. the link, the `current` link and the copies
remove_file() {
  if [ "$plan" = 1 ]; then say "  would remove $1"; return 0; fi
  rm -f "$1" || die "could not remove $1. If Strata is running, run 'strata launch --stop', then run this again."
  say "  removed $1"
}
remove_tree() {
  if [ "$plan" = 1 ]; then say "  would remove $1"; return 0; fi
  rm -rf "$1" || die "could not remove $1. If Strata is running, run 'strata launch --stop', then run this again."
  say "  removed $1"
}

if [ "$link_ours" = 1 ]; then
  remove_file "$link"
elif [ -e "$link" ] || [ -L "$link" ]; then
  say "  left $link alone (it is not a link into $terminal)"
fi

if [ "$current_is_link" = 1 ]; then
  remove_file "$current"
elif [ -e "$current" ]; then
  say "  left $current alone (a folder, not the link the installer makes)"
fi

if [ -d "$terminal" ] && [ ! -L "$terminal" ]; then
  for d in "$terminal"/*; do
    [ -e "$d" ] || continue
    [ "$d" = "$current" ] && continue
    if [ -f "$d/bin/strata" ]; then
      remove_tree "$d"
    else
      say "  left $d alone (it does not look like a Strata copy)"
    fi
  done
fi

if [ "$purge" = 1 ]; then
  for base in "$DATA_DIR" "$STATE_DIR"; do
    for sub in components runtime tuning time-skip draft-costs; do
      if [ -e "$base/$sub" ]; then remove_tree "$base/$sub"; fi
    done
  done
  if [ -z "${STRATA_HOME:-}" ] && [ -e "$HOME_DIR" ]; then remove_tree "$HOME_DIR"; fi
  if [ -e "$CONFIG_DIR" ]; then remove_tree "$CONFIG_DIR"; fi
fi

# An empty folder Strata made is removed; a folder with anything left in it stays.
if [ "$plan" = 0 ]; then
  rmdir "$terminal" 2>/dev/null || true
  if [ "$DATA_DIR" != / ] && [ "$DATA_DIR" != "$HOME" ]; then
    rmdir "$DATA_DIR" 2>/dev/null || true
  fi
fi

if [ "$plan" = 1 ]; then
  say 'Nothing was changed (--plan).'
  exit 0
fi
if [ "$installed" = 1 ]; then
  say 'Strata Terminal was removed.'
else
  say 'Strata Terminal was not installed, so there was nothing of it to remove.'
fi
if [ "$purge" = 1 ]; then
  say "  Strata data: engine components, saved state, settings and chats were removed."
else
  say "  Kept: Strata data under $DATA_DIR (add --purge to remove it)"
fi
say '  Kept: every model folder. No model or downloaded model file was touched.'
say 'To install Strata again:'
say '  curl -fsSL --connect-timeout 15 --max-time 120 https://github.com/Niko1221/Strata/releases/latest/download/install.sh -o install.sh && sh install.sh'
