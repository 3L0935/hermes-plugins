#!/usr/bin/env bash
# Install THIS plugin into the Hermes home through the repo installer.
# Same options as the root installer (--profile, --dry-run, --yes, --home, --link-only).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"
[ -x "$root/install.sh" ] || { echo "install.sh introuvable dans $root" >&2; exit 1; }
exec "$root/install.sh" "$(basename "$here")" "$@"
