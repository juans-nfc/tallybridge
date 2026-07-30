#!/usr/bin/env python3
"""
Pull packing-line files off the payroll share into TallyBridge's incoming folder.

Watches a mounted SMB share (e.g. \\\\192.168.1.10\\payroll\\STAMPER), copies any
new NF-*.txt / IL-*.txt into the incoming folder, then moves the source into an
archive subfolder on the share so it is never picked up twice.

Design notes, because this runs unattended against a network share:

* The copy into the incoming folder is atomic — written to a dot-prefixed
  temporary name and renamed into place — so the converter can never see a
  half-copied file.
* A file is only touched once its size has stopped changing, so a file still
  being written by the line system is left for the next pass.
* Everything a pass handles is recorded in a small state file. If the archive
  move fails (a read-only share, a permissions problem), the file is NOT copied
  again on the next pass — it is reported instead. Duplicate piece counts
  reaching payroll is the one outcome worth going out of the way to prevent.
* A missing or unmounted share is reported and retried, never treated as
  "the share is empty".

Usage:
    python3 share_fetch.py --source /share --dest /data/incoming \\
                           --archive /share/processed
    python3 share_fetch.py ... --once        # single pass, for cron or testing
"""

import argparse
import fnmatch
import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_PATTERNS = ("NF-*.txt", "IL-*.txt")
DEFAULT_POLL = 60          # seconds between passes
SETTLE_SECONDS = 3         # gap between the two size checks
STATE_KEEP_DAYS = 60       # how long to remember a handled file


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


# ---------------------------------------------------------------------------
# State: which source files have already been handled
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    pruned = {k: v for k, v in state.items() if v.get("when", 0) >= cutoff}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(pruned, fh, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"WARNING: could not write state file {path}: {exc}")


def fingerprint(p: Path) -> str:
    """Identify a file by name, size and mtime — a same-named file with new
    content still counts as new."""
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def matches(name: str, patterns) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pat.lower()) for pat in patterns)


def is_settled(p: Path) -> bool:
    """True once the file's size stops changing — it isn't still being written."""
    try:
        first = p.stat().st_size
        time.sleep(SETTLE_SECONDS)
        return first == p.stat().st_size and first > 0
    except OSError:
        return False


def copy_atomic(src: Path, dest_dir: Path) -> Path:
    """Copy into dest_dir under a temp name, then rename into place."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".incoming-{os.getpid()}-{src.name}"
    final = dest_dir / src.name
    shutil.copy2(src, tmp)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final


def archive_source(src: Path, archive_dir: Path) -> Path:
    """Move the source out of the way, keeping both copies on a name clash."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / src.name
    if target.exists():
        stamp = time.strftime("%Y%m%d%H%M%S")
        target = archive_dir / f"{src.stem}-{stamp}{src.suffix}"
    shutil.move(str(src), str(target))
    return target


# ---------------------------------------------------------------------------

def one_pass(source: Path, dest: Path, archive: Path, state: Path,
             patterns) -> int:
    """Copy anything new. Returns the number of files handed over."""
    if not source.is_dir():
        log(f"WARNING: share not available at {source} — is the mount up? "
            f"(check: mount | grep {source})")
        return 0

    try:
        candidates = sorted(
            p for p in source.iterdir()
            if p.is_file() and matches(p.name, patterns)
        )
    except OSError as exc:
        log(f"WARNING: cannot read {source}: {exc}")
        return 0

    seen = load_state(state)
    handed = 0

    for src in candidates:
        try:
            fp = fingerprint(src)
        except OSError as exc:
            log(f"WARNING: cannot stat {src.name}: {exc}")
            continue

        if seen.get(src.name, {}).get("fingerprint") == fp:
            # Already handled. Left on the share, so the archive move failed
            # last time — say so once per pass rather than copying it again.
            if not seen[src.name].get("archived"):
                log(f"NOTE: {src.name} was already copied but is still on the "
                    f"share — move it into {archive.name}/ by hand, or fix "
                    f"write permissions; it will not be copied again")
            continue

        if not is_settled(src):
            log(f"{src.name} is still being written — leaving it for next pass")
            continue

        if (dest / src.name).exists():
            log(f"{src.name} is already waiting in the incoming folder — "
                f"leaving it for next pass")
            continue

        try:
            copy_atomic(src, dest)
        except OSError as exc:
            log(f"ERROR copying {src.name}: {exc}")
            continue

        seen[src.name] = {"fingerprint": fp, "when": time.time(), "archived": False}
        save_state(state, seen)   # record before the move, never lose the fact
        handed += 1
        log(f"Copied {src.name} -> {dest}")

        try:
            moved_to = archive_source(src, archive)
        except OSError as exc:
            log(f"WARNING: copied {src.name} but could not move it into "
                f"{archive}: {exc} — it will not be copied again, but the "
                f"share needs tidying by hand")
        else:
            seen[src.name]["archived"] = True
            save_state(state, seen)
            log(f"Archived on the share -> {moved_to}")

    return handed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("--source", type=Path, required=True,
                    help="folder to watch (the mounted share)")
    ap.add_argument("--dest", type=Path, required=True,
                    help="TallyBridge incoming folder")
    ap.add_argument("--archive", type=Path, default=None,
                    help="where to move handled files (default: <source>/processed)")
    ap.add_argument("--state", type=Path, default=None,
                    help="state file (default: <dest>/../share-fetch-state.json)")
    ap.add_argument("--patterns", nargs="+", default=list(DEFAULT_PATTERNS))
    ap.add_argument("--poll", type=int, default=DEFAULT_POLL)
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    args = ap.parse_args()

    archive = args.archive or args.source / "processed"
    state = args.state or args.dest.parent / "share-fetch-state.json"

    if args.once:
        one_pass(args.source, args.dest, archive, state, args.patterns)
        return 0

    log(f"Watching share {args.source} for {', '.join(args.patterns)} "
        f"(every {args.poll}s), archiving to {archive}")
    while True:
        try:
            one_pass(args.source, args.dest, archive, state, args.patterns)
        except Exception as exc:                      # never die on one bad pass
            log(f"ERROR during pass: {exc}")
        time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
