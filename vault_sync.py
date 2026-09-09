#!/usr/bin/env python3
"""
vault_sync.py — wrapper around `unison <profile> -batch -dumbtty` for an Obsidian
vault kept in two copies (a Linux/WSL copy and a Windows/NTFS copy).

It auto-resolves the one conflict that Obsidian creates constantly: on the
Windows side Obsidian re-stamps the `updated:` frontmatter field while the note
body is unchanged, so a plain `unison` run flags a conflict on every recently
opened note. When only timestamp keys differ and the body is byte-identical,
this wrapper picks the Linux side and copies it across. Everything else — a real
body difference — is left for you to resolve by hand, and the exit code is 1 so
a script can tell.

Configuration is read from the environment (see examples/vault-sync.env.example):

  LOCAL_VAULT              Linux/WSL vault root         (required)
  WINDOWS_VAULT            Windows/NTFS vault root      (required)
  UNISON_PROFILE           Unison profile name          (default: vault)
  SLEEP_SECONDS            wait for Obsidian to re-stamp (default: 3)
  VAULT_SYNC_HISTORY_FILE  append one audit line per run (default: off)
"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml  # PyYAML

LOCAL_VAULT = Path(os.environ.get("LOCAL_VAULT", "")).expanduser()
WINDOWS_VAULT = Path(os.environ.get("WINDOWS_VAULT", "")).expanduser()
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "3"))
UNISON_PROFILE = os.environ.get("UNISON_PROFILE", "vault")
RACE_RETRY_SLEEP = 5

_hist = os.environ.get("VAULT_SYNC_HISTORY_FILE", "")
HISTORY_FILE = Path(_hist).expanduser() if _hist else None

# Frontmatter keys Obsidian rewrites on its own; a difference here is not a conflict.
TIMESTAMP_KEYS = {"updated", "modified", "last_modified"}

FLEXOKI = {
    "blue": "32;94;166",
    "cyan": "36;131;123",
    "green": "102;128;11",
    "yellow": "173;131;1",
    "orange": "188;82;21",
    "red": "175;48;41",
    "muted": "111;110;105",
}


def color_enabled() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def color(text: str, name: str) -> str:
    if not color_enabled():
        return text
    return f"\033[38;2;{FLEXOKI[name]}m{text}\033[0m"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict | None, str]:
    """Return (frontmatter_dict, body) or (None, text) if no YAML block found."""
    if not text.startswith("---"):
        return None, text
    # Skip opening ---\n
    after_open = text[3:]
    if after_open.startswith("\n"):
        after_open = after_open[1:]
    close = after_open.find("\n---")
    if close == -1:
        return None, text
    yaml_str = after_open[:close]
    body = after_open[close + 4:]  # skip \n---
    if body.startswith("\n"):
        body = body[1:]
    try:
        fm = yaml.safe_load(yaml_str) or {}
        if not isinstance(fm, dict):
            return None, text
    except yaml.YAMLError:
        return None, text
    return fm, body


def classify_skip(local_path: Path, windows_path: Path) -> tuple[str | None, str]:
    """
    Classify a Unison-skipped conflict.

    Returns (class, reason) where class is one of:
      "frontmatter_only" — body identical, only timestamp frontmatter keys differ
      "wsl_superset"     — the Linux body is a clean append-only superset of Windows
      None               — a real body difference, needs manual review

    Guards on both auto-resolve classes:
    - `created` must be identical on both sides.
    - Only TIMESTAMP_KEYS may differ in frontmatter.
    """
    try:
        local_text = local_path.read_text(encoding="utf-8")
        win_text = windows_path.read_text(encoding="utf-8")
    except Exception as exc:
        return None, f"read error: {exc}"

    local_fm, local_body = parse_frontmatter(local_text)
    win_fm, win_body = parse_frontmatter(win_text)

    if local_fm is None or win_fm is None:
        return None, "frontmatter not parseable"

    # Guard: created must not change
    if str(local_fm.get("created", "")) != str(win_fm.get("created", "")):
        return None, "created field differs — manual review required"

    # Guard: only timestamp keys may differ in frontmatter
    all_keys = set(local_fm.keys()) | set(win_fm.keys())
    differing = [
        k for k in all_keys
        if k not in TIMESTAMP_KEYS and local_fm.get(k) != win_fm.get(k)
    ]
    if differing:
        return None, f"non-timestamp frontmatter key(s) differ: {', '.join(differing)}"

    # Class 1: body identical — pure Obsidian timestamp re-stamp on the Windows side
    if local_body == win_body:
        return "frontmatter_only", "frontmatter-only (timestamp keys)"

    # Class 2: the Linux body is a clean superset of the Windows body at a line
    # boundary. Typical for append-only notes (logs, journal entries) where the
    # Linux side added lines and Windows only Obsidian-stamped the file.
    if (local_body.startswith(win_body)
            and len(local_body) > len(win_body)
            and win_body.endswith("\n")):
        return "wsl_superset", "Linux side is an append-only superset of Windows"

    return None, "body differs"


# ---------------------------------------------------------------------------
# Audit log (optional)
# ---------------------------------------------------------------------------

def append_to_history(transferred: str, n_skipped: str, n_failed: str,
                      auto: int, body_diff: int, race: bool) -> None:
    """Append one line to VAULT_SYNC_HISTORY_FILE, if that is configured."""
    if HISTORY_FILE is None:
        return
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    race_str = "yes" if race else "no"
    line = (f"- {ts} | xfr={transferred} skip={n_skipped} fail={n_failed}"
            f" | auto={auto} body-diff={body_diff} race={race_str}\n")
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass  # best-effort; never break the sync over the log


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_frontmatter_only(rel_path: str) -> None:
    """
    Linux wins: copy Linux -> Windows, wait for Obsidian to re-stamp `updated:`,
    then copy Windows -> Linux so both sides hold the final identical content.
    """
    local_file = LOCAL_VAULT / rel_path
    win_file = WINDOWS_VAULT / rel_path
    win_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(local_file), str(win_file))
    time.sleep(SLEEP_SECONDS)
    shutil.copy2(str(win_file), str(local_file))


# ---------------------------------------------------------------------------
# Unison output parsing
# ---------------------------------------------------------------------------

_BAR_WIDTH = 30
_SCAN_PAT = re.compile(r'^[/\\|<>\-] ')
_SYNC_COUNT_RE = re.compile(r"(\d+) items? will be synced")
_SUMMARY_RE = re.compile(r"(\d+) items? transferred, (\d+) skipped, (\d+) failed")


def _progress_bar(done: int, total: int, color_name: str | None = None) -> str:
    if total <= 0:
        bar = "█" * _BAR_WIDTH
        return color(bar, color_name) if color_name else bar
    done = max(0, min(done, total))
    filled = int(_BAR_WIDTH * done / total)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    return color(bar, color_name) if color_name else bar


def _summary_count(count: str, label: str, alert_color: str) -> str:
    text = f"{count} {label}"
    if count == "?":
        return color(text, "yellow")
    try:
        value = int(count)
    except ValueError:
        return text
    if label == "transferred" and value > 0:
        return color(text, "green")
    if label != "transferred" and value > 0:
        return color(text, alert_color)
    return color(text, "muted")


def run_unison() -> tuple[str, str, int]:
    """Run Unison with a two-phase progress display.

    Phase 1 — reconciliation scan: rising progress while Unison scans.
    Phase 2 — transfer: a 0-100% bar based on "N item(s) will be synced".
    Conflicts and failures always break to their own line.
    """
    proc = subprocess.Popen(
        ["unison", UNISON_PROFILE, "-batch", "-dumbtty"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    lines: list[str] = []
    total_xfr = 0  # from "N items will be synced"
    done_xfr = 0
    scan_ticks = 0
    phase = "scan"

    print(
        f"\r  {color('Scanning', 'cyan')} "
        f"[{_progress_bar(0, 100, 'cyan')}] 0%",
        end="",
        flush=True,
    )

    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        stripped = line.strip()

        if phase == "scan":
            if _SCAN_PAT.match(stripped):
                scan_ticks += 1
                pct = min(95, scan_ticks)
                print(
                    f"\r  {color('Scanning', 'cyan')} "
                    f"[{_progress_bar(pct, 100, 'cyan')}] {pct}%",
                    end="",
                    flush=True,
                )
                continue

            m = _SYNC_COUNT_RE.search(stripped)
            if m:
                total_xfr = int(m.group(1))
                phase = "transfer"
                print(
                    f"\r  {color('Scanning', 'cyan')} "
                    f"[{_progress_bar(100, 100, 'cyan')}] 100%"
                )
                if total_xfr > 0:
                    print(
                        f"\r  {color('Syncing ', 'green')} "
                        f"[{_progress_bar(0, total_xfr, 'green')}] 0% "
                        f"(0/{total_xfr})",
                        end="",
                        flush=True,
                    )
                continue

        elif phase == "transfer":
            if stripped.startswith("[BGN]") and total_xfr > 0:
                done_xfr += 1
                shown_done = min(done_xfr, total_xfr)
                pct = int(shown_done * 100 / total_xfr)
                print(
                    f"\r  {color('Syncing ', 'green')} "
                    f"[{_progress_bar(shown_done, total_xfr, 'green')}] "
                    f"{pct}% ({shown_done}/{total_xfr})",
                    end="",
                    flush=True,
                )
                continue

        # Conflicts and failures always get their own line
        if stripped.startswith(("[CONFLICT]", "Failed [")):
            print()
            line_color = "yellow" if stripped.startswith("[CONFLICT]") else "red"
            print(f"  {color(stripped, line_color)}", flush=True)

    proc.wait()
    print()
    return "".join(lines), "", proc.returncode


def parse_output(stdout: str, stderr: str) -> tuple[list[str], list[str], tuple[str, str, str]]:
    """
    Parse Unison's combined output.

    Returns:
      skipped  — relative paths that were [CONFLICT] Skipped
      failed   — relative paths that Failed (modified during sync)
      summary  — (transferred, skipped_count, failed_count) as strings
    """
    combined = stdout + "\n" + stderr

    # Conflicts appear in two places:
    # 1. During propagation:  "[CONFLICT] Skipping <path>"  (only when other items also transfer)
    # 2. Summary section:     "  skipped: <path> (contents changed on both sides)"  (always)
    # The summary lines are the primary source — they cover the "only conflicts" case.
    skipped_summary = re.findall(r"^\s+skipped: (.+?) \(", combined, re.MULTILINE)
    skipped_inline = re.findall(r"^\[CONFLICT\] Skipping (.+)$", combined, re.MULTILINE)
    seen: set[str] = set()
    skipped: list[str] = []
    for p in skipped_summary + skipped_inline:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            skipped.append(p)

    # Failed [<path>]: ... — covers race-failures and any other failure
    failed = re.findall(r"^Failed \[(.+?)\]", combined, re.MULTILINE)
    failed = [p.strip() for p in failed]

    m = _SUMMARY_RE.search(combined)
    summary = m.groups() if m else ("?", "?", "?")

    return skipped, failed, summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def classify_and_resolve(rel_path: str,
                         auto_resolved: list[str],
                         body_diffs: list[tuple[str, str]]) -> None:
    """Classify one skipped path and either auto-resolve it or queue it for review."""
    local_file = LOCAL_VAULT / rel_path
    win_file = WINDOWS_VAULT / rel_path

    if not local_file.exists() or not win_file.exists():
        body_diffs.append((rel_path, "one side missing — check manually"))
        return

    cls, reason = classify_skip(local_file, win_file)
    if cls in ("frontmatter_only", "wsl_superset"):
        try:
            resolve_frontmatter_only(rel_path)
            auto_resolved.append(rel_path)
        except Exception as exc:
            body_diffs.append((rel_path, f"resolve error: {exc}"))
    else:
        body_diffs.append((rel_path, reason))


def main() -> None:
    if not os.environ.get("LOCAL_VAULT") or not os.environ.get("WINDOWS_VAULT"):
        sys.exit("Set LOCAL_VAULT and WINDOWS_VAULT — see examples/vault-sync.env.example")

    print(color("Running vault-sync...", "blue"), flush=True)
    stdout, stderr, _rc = run_unison()
    skipped, failed, (transferred, n_skipped, n_failed) = parse_output(stdout, stderr)

    auto_resolved: list[str] = []
    body_diffs: list[tuple[str, str]] = []

    for rel_path in skipped:
        classify_and_resolve(rel_path, auto_resolved, body_diffs)

    # Retry once on race-failures (Obsidian was writing during the sync)
    race_retry_status: str = ""
    if failed:
        print(
            f"  {color('Race detected', 'yellow')} on {len(failed)} file(s) "
            f"— retrying in {RACE_RETRY_SLEEP}s...",
            flush=True,
        )
        time.sleep(RACE_RETRY_SLEEP)
        r_stdout, r_stderr, _r_rc = run_unison()
        r_skipped, r_failed, (transferred, n_skipped, n_failed) = parse_output(r_stdout, r_stderr)
        if not r_failed:
            race_retry_status = f"succeeded on retry ({len(failed)} file(s))"
            seen = {p for p, _ in body_diffs} | set(auto_resolved)
            for rel_path in r_skipped:
                if rel_path not in seen:
                    classify_and_resolve(rel_path, auto_resolved, body_diffs)
        else:
            race_retry_status = "still failing — close Obsidian on Windows and re-run"

    # Report
    print(
        f"\n{color('vault-sync:', 'blue')} "
        f"{_summary_count(transferred, 'transferred', 'green')}, "
        f"{_summary_count(n_skipped, 'skipped', 'yellow')}, "
        f"{_summary_count(n_failed, 'failed', 'red')}"
    )
    if auto_resolved:
        print(
            f"{color('auto-resolved:', 'green')}                    "
            f"{len(auto_resolved)} file(s)"
        )
        for p in auto_resolved:
            print(f"  {color('+', 'green')} {p}")
    if body_diffs:
        print(
            f"{color('needs review (body-diff):', 'red')}         "
            f"{len(body_diffs)} file(s)"
        )
        for p, reason in body_diffs:
            print(f"  {color('!', 'red')} {p}")
            print(f"      {color(f'({reason})', 'muted')}")
        print()
        print(f"  {color('Resolve the file(s) above by hand, then re-run.', 'orange')}")
    if race_retry_status:
        print(f"{color('race-retry:', 'yellow')} {race_retry_status}")
    if not auto_resolved and not body_diffs and not race_retry_status:
        print(f"  {color('(no conflicts)', 'muted')}")

    append_to_history(transferred, n_skipped, n_failed,
                      len(auto_resolved), len(body_diffs), bool(failed))

    sys.exit(1 if body_diffs else 0)


if __name__ == "__main__":
    main()
