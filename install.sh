#!/usr/bin/env bash
# Install obsidian-wsl-sync for the current user: a venv for the wrapper,
# symlinks in ~/.local/bin, a config file, and the systemd --user units.
# It does not enable anything — edit the config first, then enable by hand.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="$HOME/.local/bin"
confdir="${XDG_CONFIG_HOME:-$HOME/.config}/vault-sync"
unitdir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
venv="$HOME/.local/share/vault-sync/venv"

command -v unison >/dev/null || { echo "error: unison not found — install it first"; exit 1; }
command -v inotifywait >/dev/null || echo "warning: inotifywait not found — the watch daemon needs inotify-tools"
command -v flock >/dev/null || { echo "error: flock not found (util-linux)"; exit 1; }

echo "venv        -> $venv"
python3 -m venv "$venv"
"$venv/bin/pip" -q install -r "$here/requirements.txt"

echo "symlinks    -> $bindir"
mkdir -p "$bindir"
chmod +x "$here/bin/vault-sync" "$here/bin/vault-sync-watch"
ln -sf "$here/bin/vault-sync"       "$bindir/vault-sync"
ln -sf "$here/bin/vault-sync-watch" "$bindir/vault-sync-watch"

mkdir -p "$confdir"
if [ -f "$confdir/vault-sync.env" ]; then
    echo "config      -> $confdir/vault-sync.env (kept; not overwritten)"
else
    cp "$here/examples/vault-sync.env.example" "$confdir/vault-sync.env"
    echo "config      -> $confdir/vault-sync.env (created — edit before starting)"
fi

echo "units       -> $unitdir"
mkdir -p "$unitdir"
cp "$here/examples/vault-sync.service" "$here/examples/vault-sync-watch.service" \
   "$here/examples/vault-sync.timer" "$unitdir/"
systemctl --user daemon-reload 2>/dev/null || true

cat <<EOF

Done. Next steps:
  1. Edit  $confdir/vault-sync.env
  2. Create ~/.unison/<UNISON_PROFILE>.prf  (see $here/examples/unison.prf.example)
  3. Run   vault-sync   once by hand and check the output
  4. systemctl --user enable --now vault-sync-watch.service vault-sync.timer

Check it is running:
  systemctl --user status vault-sync-watch.service
  journalctl --user -u vault-sync-watch.service -f
EOF
