# obsidian-wsl-sync

Keep an Obsidian vault in sync between WSL and Windows with low latency, so you
can edit with tools on the Linux side and watch the changes land in the Windows
Obsidian app a couple of seconds later.

It is two pieces on top of [Unison](https://github.com/bcpierce00/unison):

- **`vault-sync`** — a wrapper that runs Unison once and auto-resolves the
  frontmatter-only conflicts Obsidian creates on its own. Real content
  conflicts are left for you and reported with a non-zero exit code.
- **`vault-sync-watch`** — a daemon that watches the Linux copy with
  `inotify` and syncs each change within a couple of seconds, plus a slower
  poll for the other direction.

## Why

The common setup is one copy of the vault on the Windows filesystem and the
tools (git, editors, scripts) reaching it from WSL over `/mnt/c`. Every small
file operation then crosses the 9p bridge, which is slow, and `inotify` does not
fire for Windows-side writes.

Two native copies with Unison in between fixes the speed problem but adds
latency and a steady drip of conflicts:

- A plain `unison` run scans the whole tree. On a few thousand files over 9p
  that is tens of seconds when the attribute cache is cold.
- Obsidian on Windows re-stamps the `updated:` frontmatter field whenever you
  open a note, so a sync that caught the Linux version first sees "both sides
  changed" and skips the file.

`vault-sync-watch` cuts the latency by syncing only the paths `inotify` names
(`unison -path <file>`), which skips the full scan. `vault-sync` handles the
`updated:` churn by comparing the note bodies: if only timestamp keys differ and
the body is byte-identical, it picks the Linux side.

## How it works

```
Linux/WSL copy  --(inotify: scoped `unison -path`, ~2s)-->  Windows/NTFS copy
Linux/WSL copy  <--(full `unison` on a 15s idle poll)-----  Windows/NTFS copy
                         hourly: `vault-sync` sweep
                    (frontmatter auto-resolve + audit line)
```

- The daemon runs **raw** `unison`. It never calls `vault-sync`, because the
  wrapper writes an audit line on every run and that file would sync back and
  trigger the next run.
- The hourly sweep runs `vault-sync` under the same `flock`, so the two never
  run `unison` at the same time. It mops up any frontmatter conflicts the raw
  daemon skipped and writes one audit line.
- With ~2s propagation, frontmatter conflicts are rare, so hourly is enough.
- The daemon reads the profile's `ignore = Path` / `BelowPath` lines and tells
  `inotifywait` not to watch those subtrees, so the watch set and the sync set
  stay in step. `VAULT_SYNC_NOWATCH` adds more.

## Requirements

- WSL2, or any setup with a Linux side and a Windows/NTFS side
- Unison 2.53+ on `PATH`, same version on both roots
- `inotify-tools` (for `inotifywait`)
- `flock` (util-linux)
- Python 3.9+. With [uv](https://docs.astral.sh/uv/) installed the wrapper runs
  `uv run` and needs nothing else (PyYAML comes from the script's PEP 723
  header). Without uv, `install.sh` builds a small venv.

`unison-fsmonitor` (which would give `unison -repeat watch`) is not packaged for
2.53 on current Ubuntu, which is why this uses `inotifywait` directly.

## Install

```bash
git clone https://github.com/gavinsdavies/obsidian-wsl-sync ~/github/obsidian-wsl-sync
cd ~/github/obsidian-wsl-sync
./install.sh
```

`install.sh` symlinks `vault-sync` and `vault-sync-watch` into `~/.local/bin`,
drops a config file at `~/.config/vault-sync/vault-sync.env`, and copies the
systemd `--user` units. If `uv` is not installed it also builds a venv for
PyYAML. It does not start anything.

To pick up later changes: `git pull` in the clone and
`systemctl --user restart vault-sync-watch.service` — the `~/.local/bin`
symlinks point straight at the repo.

Then:

1. Edit `~/.config/vault-sync/vault-sync.env` — set `LOCAL_VAULT`,
   `WINDOWS_VAULT`, `UNISON_PROFILE`.
2. Create `~/.unison/<UNISON_PROFILE>.prf` from
   [`examples/unison.prf.example`](examples/unison.prf.example).
3. Run `vault-sync` once by hand and read the output.
4. Enable the units:

```bash
systemctl --user enable --now vault-sync-watch.service vault-sync.timer
```

No `enable-linger`, so nothing runs while you are logged out.

## Configuration

All settings live in `~/.config/vault-sync/vault-sync.env` (shell syntax). See
[`examples/vault-sync.env.example`](examples/vault-sync.env.example) for the full
list. The ones you must set:

| Variable | Meaning |
|---|---|
| `LOCAL_VAULT` | the Linux/WSL vault root (native filesystem) |
| `WINDOWS_VAULT` | the Windows vault root, e.g. `/mnt/c/Users/you/...` |
| `UNISON_PROFILE` | profile name; must match `~/.unison/<name>.prf` |
| `VAULT_SYNC_PYTHON` | Python with PyYAML (the venv `install.sh` builds) |

## Status badge

Set `VAULT_SYNC_STATUS_FILE` and the daemon writes a callout to that file:

```
> [!success] Vault synced
> `2026-09-08 20:42:05` · Linux <-> Windows sync
```

`[!warning]` while starting or when a sync needs review, `[!failure]` if the
watcher dies. Embed it in an Obsidian note with `![[.sync-status]]` for a
green check that shows the sync is alive. The file is excluded from the watch
(so writing it does not trigger a sync) and reaches the Windows side on the
next poll, so its timestamp there can lag 15–30s.

## Checking it is running

```bash
systemctl --user is-active vault-sync-watch.service      # -> active
systemctl --user status  vault-sync-watch.service        # uptime, restarts, last log
pgrep -af 'inotifywait -m -r'                            # the watcher process
journalctl --user -u vault-sync-watch.service -f         # live
systemctl --user list-timers vault-sync.timer            # next hourly sweep
```

## Scope and limits

- Built for Unison 2.53.3 on WSL2. The `[CONFLICT]` / `Failed [...]` log format
  is version-specific.
- The Windows→Linux direction is a poll (15s + scan), because `inotify` does
  not see `/mnt/c` writes over 9p. Edits made in Obsidian on Windows take
  15–30s to reach the Linux side.
- Auto-resolution only touches conflicts where the sole difference is a
  timestamp frontmatter key (`updated`, `modified`, `last_modified`) and
  `created` is unchanged. Anything else stops for manual review.
- `confirmbigdel = true` in the profile makes `unison` refuse a large deletion
  in batch mode rather than prompt. The daemon logs the failure and retries on
  the next pass.

## Tests

```bash
python3 -m unittest test_vault_sync.py
```

## Credits

The `vault-sync` wrapper is by Gavin S. Davies. The `vault-sync-watch` daemon,
the packaging, and this documentation were built together with Anthropic's
Claude (Claude Code).

## License

MIT — see [LICENSE](LICENSE).
