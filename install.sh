#!/usr/bin/env bash
# Hermes plugin installer: link plugins from this repo into a Hermes home, enable them, and
# enable the toolsets they expose — without copying anything (symlink, so edits are live).
#
#   ./install.sh                       interactive menu
#   ./install.sh <plugin> [<plugin>]   install by name
#   ./install.sh --all                 install every plugin in this repo
#   ./install.sh --list                what is in the repo + link state
#   ./install.sh --status              detailed state (link, enabled, toolsets, env)
#   ./install.sh --uninstall <plugin>  disable + remove the link (never deletes a real dir)
#
# Options: --profile NAME | --home PATH | --link-only | --no-tools | --dry-run | --yes
#
# Design notes:
#   * No plugin list is hardcoded: a plugin is any subdirectory with a plugin.yaml.
#   * Toolsets are discovered, not declared here: whatever new "Plugin toolsets" entry
#     appears after enabling a plugin is the toolset it exposes.
#   * A real directory at the target path is never silently replaced: it is moved aside
#     (backup) or the plugin is skipped, because a stale copy would shadow this repo.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$REPO_DIR/$(basename "${BASH_SOURCE[0]}")"

PROFILE=""
HERMES_HOME_ARG=""
MODE="interactive"
DO_ENABLE=1
DO_TOOLS=1
DRY_RUN=0
ASSUME_YES=0
TARGETS=()

# --------------------------------------------------------------------------- output helpers
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi
ok()   { printf '%s ok %s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s !! %s %s\n' "$C_WARN" "$C_OFF" "$*"; }
err()  { printf '%s XX %s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; }
step() { printf '%s -- %s %s\n' "$C_DIM" "$C_OFF" "$*"; }
die()  { err "$*"; exit 1; }

usage() { sed -n '2,18p' "$SELF" | sed 's/^# \{0,1\}//'; }

# --------------------------------------------------------------------------- arguments
while [ $# -gt 0 ]; do
  case "$1" in
    --profile|-p)  PROFILE="${2:-}"; [ -n "$PROFILE" ] || die "--profile needs a name"; shift 2 ;;
    --home)        HERMES_HOME_ARG="${2:-}"; [ -n "$HERMES_HOME_ARG" ] || die "--home needs a path"; shift 2 ;;
    --link-only)   DO_ENABLE=0; DO_TOOLS=0; shift ;;
    --no-tools)    DO_TOOLS=0; shift ;;
    --dry-run|-n)  DRY_RUN=1; shift ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    --list|--ls)   MODE="list"; shift ;;
    --status)      MODE="status"; shift ;;
    --all)         MODE="install"; TARGETS=("__all__"); shift ;;
    --uninstall)   MODE="uninstall"; shift
                   if [ "${1:-}" = "--all" ]; then TARGETS=("__all__"); shift; fi ;;
    --help|-h)     usage; exit 0 ;;
    -*)            die "option inconnue: $1 (--help)" ;;
    *)             [ "$MODE" = "uninstall" ] || MODE="install"; TARGETS+=("$1"); shift ;;
  esac
done

BASE_HOME="${HERMES_HOME_ARG:-${HERMES_HOME:-$HOME/.hermes}}"
BASE_HOME="${BASE_HOME/#\~/$HOME}"
if [ -n "$PROFILE" ]; then HHOME="$BASE_HOME/profiles/$PROFILE"; else HHOME="$BASE_HOME"; fi
PLUGINS_DIR="$HHOME/plugins"
[ -d "$HHOME" ] || die "home Hermes introuvable: $HHOME"

HERMES_BIN="$(command -v hermes || true)"
[ -n "$HERMES_BIN" ] || die "commande 'hermes' introuvable dans le PATH"
if [ -n "$PROFILE" ]; then
  HERMES() { "$HERMES_BIN" -p "$PROFILE" "$@"; }
else
  HERMES() { "$HERMES_BIN" "$@"; }
fi

PY=""
for candidate in "${HERMES_INSTALL_PY:-}" "$HHOME/hermes-agent/venv/bin/python3" \
                 "$BASE_HOME/hermes-agent/venv/bin/python3" "$(command -v python3 || true)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && { PY="$candidate"; break; }
done
[ -n "$PY" ] || die "aucun python3 utilisable pour lire les manifests"

# --------------------------------------------------------------------------- discovery
plugin_dirs() {
  local d
  for d in "$REPO_DIR"/*/; do
    d="${d%/}"
    [ -f "$d/plugin.yaml" ] && printf '%s\n' "$d"
  done
}

# one manifest field at a time; lists (requires_env) come back newline separated
manifest_field() {
  "$PY" - "$1/plugin.yaml" "$2" <<'PY' 2>/dev/null
import sys
path, field = sys.argv[1], sys.argv[2]
raw = open(path, encoding="utf-8-sig").read()
try:
    import yaml
    data = yaml.safe_load(raw) or {}
except Exception:
    data = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(field + ":"):
            data[field] = stripped.split(":", 1)[1].strip().strip("\"'")
value = data.get(field)
if value is None:
    raise SystemExit(0)
if isinstance(value, (list, tuple)):
    print("\n".join(str(item) for item in value))
else:
    print(str(value).strip())
PY
}

plugin_name() {
  local name
  name="$(manifest_field "$1" name)"
  printf '%s\n' "${name:-$(basename "$1")}"
}

find_plugin_dir() {
  local d
  for d in $(plugin_dirs); do
    if [ "$(basename "$d")" = "$1" ] || [ "$(plugin_name "$d")" = "$1" ]; then
      printf '%s\n' "$d"; return 0
    fi
  done
  return 1
}

link_state() { # $1 name, $2 source dir
  local dst="$PLUGINS_DIR/$1"
  if [ -L "$dst" ]; then
    local tgt
    tgt="$(readlink -f "$dst" 2>/dev/null || true)"
    if [ -n "$tgt" ] && [ "$tgt" = "$(readlink -f "$2" 2>/dev/null)" ]; then echo "linked"; else echo "other"; fi
  elif [ -d "$dst" ]; then echo "copy"
  else echo "missing"
  fi
}

enabled_state() { # $1 name -> enabled|disabled|unknown
  local json
  json="$(HERMES plugins list --json 2>/dev/null)" || { echo "unknown"; return; }
  printf '%s' "$json" | "$PY" -c '
import json, sys
name = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except Exception:
    print("unknown"); raise SystemExit
for row in rows:
    if row.get("name") == name:
        print("enabled" if row.get("status") == "enabled" else "disabled")
        raise SystemExit
print("unknown")
' "$1" 2>/dev/null || echo "unknown"
}

platforms() {
  "$PY" - "$HHOME/config.yaml" <<'PY' 2>/dev/null
import sys
order = []
try:
    import yaml
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8-sig").read()) or {}
    order = list((cfg.get("platform_toolsets") or {}).keys())
except Exception:
    pass
seen = []
for name in order + ["cli"]:
    if name and name not in seen:
        seen.append(name)
print("\n".join(seen))
PY
}

plugin_toolsets() { # $1 platform -> toolset keys owned by plugins (enabled or disabled)
  HERMES tools list --platform "$1" 2>/dev/null \
    | sed -n '/^Plugin toolsets/,$p' \
    | awk 'NF >= 3 && ($1 == "✓" || $1 == "✗") {print $3}'
}

all_plugin_toolsets() { # every plugin toolset known on any platform, sorted unique
  local p
  for p in $(platforms); do plugin_toolsets "$p"; done | sort -u
}

toolset_state() { # $1 platform, $2 toolset -> enabled|disabled|absent
  if [ -n "${TOOLSET_CACHE:-}" ] && [ -f "$TOOLSET_CACHE" ]; then
    local cached
    cached="$(awk -v p="$1" -v t="$2" '$1 == p && $2 == t {print $3; exit}' "$TOOLSET_CACHE")"
    if [ -n "$cached" ]; then echo "$cached"; else echo "absent"; fi
    return
  fi
  local line
  line="$(HERMES tools list --platform "$1" 2>/dev/null \
    | grep -E "^[[:space:]]*(✓|✗)[[:space:]]+(enabled|disabled)[[:space:]]+$2([[:space:]]|$)" | head -1)"
  case "$line" in
    *disabled*) echo "disabled" ;;
    "")         echo "absent" ;;
    *)          echo "enabled" ;;
  esac
}

# one `hermes tools list` per platform, cached as "platform toolset state"
refresh_toolsets_cache() {
  TOOLSET_CACHE="$(mktemp)"
  local p
  for p in $(platforms); do
    HERMES tools list --platform "$p" 2>/dev/null \
      | sed -n '/^Plugin toolsets/,$p' \
      | awk -v plat="$p" 'NF >= 3 && $1 == "✓" {print plat, $3, "enabled"}
                          NF >= 3 && $1 == "✗" {print plat, $3, "disabled"}' >>"$TOOLSET_CACHE"
  done
}

env_present() { # $1 VAR
  [ -n "${!1:-}" ] && return 0
  [ -f "$HHOME/.env" ] && grep -qE "^[[:space:]]*$1[[:space:]]*=" "$HHOME/.env"
}

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  local answer
  read -r -p "$1 [o/N] " answer || return 1
  case "$answer" in o|O|y|Y|oui|Oui) return 0 ;; *) return 1 ;; esac
}

# --------------------------------------------------------------------------- read-only views
list_plugins() {
  printf '%-22s %-10s %s\n' "PLUGIN" "LINK" "DESCRIPTION"
  local d name desc
  for d in $(plugin_dirs); do
    name="$(plugin_name "$d")"
    printf '%-22s %-10s %s\n' "$name" "$(link_state "$name" "$d")" \
      "$(manifest_field "$d" description | tr '\n' ' ' | cut -c1-58)"
  done
  printf '\nrepo: %s\nhome: %s%s\n' "$REPO_DIR" "$HHOME" "${PROFILE:+ (profil $PROFILE)}"
}

status_plugins() {
  local d name req ts row total disabled
  refresh_toolsets_cache
  for d in $(plugin_dirs); do
    name="$(plugin_name "$d")"
    printf '\n%s\n  lien     %s\n  plugin   %s\n' "$name" \
      "$(link_state "$name" "$d")" "$(enabled_state "$name")"
    for req in $(manifest_field "$d" requires_env); do
      if env_present "$req"; then printf '  env      %s ok\n' "$req"
      else printf '  env      %s MANQUANT\n' "$req"; fi
    done
  done
  printf '\nToolsets de plugins (hermes tools, tous plugins confondus)\n'
  for ts in $(awk '$2 != "" {print $2}' "$TOOLSET_CACHE" | sort -u); do
    row="$(awk -v t="$ts" '$2 == t {print $1, $3}' "$TOOLSET_CACHE")"
    [ -n "$row" ] || continue
    total="$(printf '%s\n' "$row" | wc -l | tr -d ' ')"
    disabled="$(printf '%s\n' "$row" | grep -c ' disabled$' || true)"
    if [ "$disabled" = 0 ]; then
      printf '  %-16s enabled (%s/%s)\n' "$ts" "$total" "$total"
    else
      printf '  %-16s disabled sur %s/%s: %s\n' "$ts" "$disabled" "$total" \
        "$(printf '%s\n' "$row" | awk '$2 == "disabled" {printf "%s ", $1}')"
    fi
  done
  rm -f "$TOOLSET_CACHE"; TOOLSET_CACHE=""
}

# --------------------------------------------------------------------------- actions
link_plugin() { # $1 name, $2 source dir
  local name="$1" src="$2" dst="$PLUGINS_DIR/$name" state tgt bak
  state="$(link_state "$name" "$src")"
  case "$state" in
    linked) step "lien deja en place -> $src"; return 0 ;;
    other)
      tgt="$(readlink "$dst" 2>/dev/null)"
      [ "$DRY_RUN" = 1 ] && { step "[dry-run] ln -sfn $src $dst (remplace -> $tgt)"; return 0; }
      warn "$dst pointe ailleurs ($tgt)"
      if [ "$ASSUME_YES" = 1 ] || confirm "remplacer ce lien ?"; then
        ln -sfn "$src" "$dst" && ok "lien remplace -> $src" && return 0
        err "echec: $dst"; return 1
      fi
      warn "lien conserve, plugin ignore"; return 1 ;;
    copy)
      bak="$dst.bak-$(date +%Y%m%d-%H%M%S)"
      [ "$DRY_RUN" = 1 ] && { step "[dry-run] mv $dst $(basename "$bak") && ln -sfn $src $dst"; return 0; }
      warn "$dst est un dossier reel: il masquerait ce repo"
      if [ "$ASSUME_YES" = 1 ] || confirm "deplacer en $(basename "$bak") puis creer le lien ?"; then
        if mv "$dst" "$bak" && ln -sfn "$src" "$dst"; then ok "copie archivee ($bak), lien cree"; return 0; fi
        err "echec du remplacement de $dst"; return 1
      fi
      warn "plugin ignore (copie existante)"; return 1 ;;
    missing)
      [ "$DRY_RUN" = 1 ] && { step "[dry-run] ln -sfn $src $dst"; return 0; }
      mkdir -p "$PLUGINS_DIR" && ln -sfn "$src" "$dst" && ok "lien -> $src" && return 0
      err "impossible de creer $dst"; return 1 ;;
  esac
}

enable_plugin() {
  if [ "$(enabled_state "$1")" = "enabled" ]; then step "plugin deja active"; return 0; fi
  [ "$DRY_RUN" = 1 ] && { step "[dry-run] hermes plugins enable $1"; return 0; }
  local out
  out="$(HERMES plugins enable "$1" 2>&1)" || { err "activation echouee: $out"; return 1; }
  ok "plugin active"
}

# Toolset registration is global (a tool's toolset does not depend on the platform), so one
# `hermes tools list --platform cli` per plugin is enough to discover what it exposes. That
# keeps `--all` from paying 8 platforms x N plugins of CLI startup.
cli_plugin_toolsets() { plugin_toolsets cli | sort -u; }

enable_new_toolsets() { # $1 before snapshot, $2 after snapshot (both cli, sorted)
  local before="$1" after="$2" p ts done_on=() failed=()
  [ "$DO_TOOLS" = 1 ] || { rm -f "$before" "$after"; return 0; }
  local new
  new="$(comm -13 "$before" "$after" 2>/dev/null)"
  rm -f "$before" "$after"
  if [ -z "$new" ]; then step "aucun nouveau toolset (plugin sans tools, ou deja connu)"; return 0; fi
  for ts in $new; do
    done_on=(); failed=()
    for p in $(platforms); do
      if [ "$DRY_RUN" = 1 ]; then done_on+=("$p"); continue; fi
      # idempotent: hermes answers "already enabled" for platforms that already have it
      if HERMES tools enable --platform "$p" "$ts" >/dev/null 2>&1; then done_on+=("$p")
      else failed+=("$p"); fi
    done
    if [ "$DRY_RUN" = 1 ]; then
      step "[dry-run] hermes tools enable --platform <plat> $ts  (${done_on[*]})"
    else
      [ "${#done_on[@]}" -gt 0 ] && ok "toolset $ts active sur: ${done_on[*]}"
      [ "${#failed[@]}" -gt 0 ] && warn "toolset $ts NON active sur: ${failed[*]} (a faire a la main)"
    fi
  done
}

install_plugin() {
  local name="$1" dir rc=0 before after req
  dir="$(find_plugin_dir "$name")" || { err "plugin inconnu: $name"; return 1; }
  name="$(plugin_name "$dir")"   # canonical name from the manifest, not the directory
  printf '\n=== %s\n' "$name"
  before="$(mktemp)"; cli_plugin_toolsets >"$before" 2>/dev/null
  link_plugin "$name" "$dir" || rc=1
  if [ "$rc" = 0 ] && [ "$DO_ENABLE" = 1 ]; then
    if enable_plugin "$name"; then
      if [ "$DRY_RUN" = 1 ]; then rm -f "$before"
      else
        after="$(mktemp)"; cli_plugin_toolsets >"$after" 2>/dev/null
        enable_new_toolsets "$before" "$after"
      fi
    else
      rm -f "$before"; rc=1
    fi
  else
    rm -f "$before"
  fi
  for req in $(manifest_field "$dir" requires_env); do
    env_present "$req" || warn "$req non defini (a mettre dans $HHOME/.env)"
  done
  return "$rc"
}

uninstall_plugin() {
  local name="$1" dst
  if local dir; dir="$(find_plugin_dir "$name")"; then name="$(plugin_name "$dir")"; fi
  dst="$PLUGINS_DIR/$name"
  printf '\n=== %s\n' "$name"
  if [ "$(enabled_state "$name")" = "enabled" ]; then
    if [ "$DRY_RUN" = 1 ]; then step "[dry-run] hermes plugins disable $name"
    elif HERMES plugins disable "$name" >/dev/null 2>&1; then ok "plugin desactive"; fi
  fi
  if [ -L "$dst" ]; then
    if [ "$DRY_RUN" = 1 ]; then step "[dry-run] rm $dst"; else rm -f "$dst" && ok "lien retire"; fi
  elif [ -d "$dst" ]; then
    warn "$dst est un dossier reel, non touche"
  else
    step "aucun lien a retirer"
  fi
}

menu() {
  local d i pick dirs=() names=() states=() chosen=()
  for d in $(plugin_dirs); do
    dirs+=("$d")
    names+=("$(plugin_name "$d")")
    states+=("$(link_state "$(plugin_name "$d")" "$d")")
  done
  [ "${#names[@]}" -gt 0 ] || die "aucun plugin trouve dans $REPO_DIR"
  printf '\nInstallateur de plugins Hermes\nrepo: %s\nhome: %s\n\n' "$REPO_DIR" "$HHOME"
  for i in "${!names[@]}"; do
    printf '  %d) %-22s [%-7s] %s\n' "$((i + 1))" "${names[$i]}" "${states[$i]}" \
      "$(manifest_field "${dirs[$i]}" description | tr '\n' ' ' | cut -c1-46)"
  done
  printf '\n  0) tout\n  q) quitter\n\n'
  read -r -p "Selection (ex: 1 3, ou 0) : " pick || exit 0
  case "$pick" in q|Q) exit 0 ;; esac
  for i in $pick; do
    if [ "$i" = "0" ]; then chosen=("${names[@]}"); break; fi
    if ! [[ "$i" =~ ^[0-9]+$ ]] || [ "$i" -lt 1 ] || [ "$i" -gt "${#names[@]}" ]; then
      die "selection invalide: $i"
    fi
    chosen+=("${names[$((i - 1))]}")
  done
  [ "${#chosen[@]}" -gt 0 ] || die "rien de selectionne"
  printf '\nCible : %s\nMode  : %s\n' "${chosen[*]}" \
    "$([ "$DO_ENABLE" = 1 ] && echo 'lien + activation plugin + toolsets' || echo 'lien seulement')"
  [ "$DRY_RUN" = 1 ] && printf 'DRY-RUN: aucune modification\n'
  confirm "On y va ?" || { printf 'annule\n'; exit 0; }
  TARGETS=("${chosen[@]}")
}

# --------------------------------------------------------------------------- main
case "$MODE" in
  list)        list_plugins; exit 0 ;;
  status)      status_plugins; exit 0 ;;
  interactive) [ -t 0 ] || { usage; exit 0; }; menu ;;
esac

[ "${#TARGETS[@]}" -gt 0 ] || die "aucune cible (--help)"
if [ "${TARGETS[0]}" = "__all__" ]; then
  TARGETS=()
  for d in $(plugin_dirs); do TARGETS+=("$(plugin_name "$d")"); done
fi

fail=0
for t in "${TARGETS[@]}"; do
  if [ "$MODE" = "uninstall" ]; then uninstall_plugin "$t" || fail=1
  else install_plugin "$t" || fail=1; fi
done

printf '\n'
[ "$fail" = 0 ] && ok "termine" || warn "termine avec des erreurs"
if [ "$MODE" = "install" ] && [ "$DO_ENABLE" = 1 ]; then
  printf 'Prochaine etape: redemarrer la gateway (hooks) puis ouvrir une nouvelle session\n'
  printf '(les tools ne sont visibles qu a partir d une nouvelle conversation).\n'
fi
exit "$fail"
