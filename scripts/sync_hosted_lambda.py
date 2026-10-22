#!/usr/bin/env python3
"""Copy this repo's lambda/ into a clone of the Alexa-hosted skill.

The code directory is whichever hosted directory contains lambda_function.py.
The weekly timetable refresh owns schedule.bin and feed.json, so those files
are never written or deleted. skill.json is never written. requirements.txt
is updated only when the dependency lines already match; a different set is
printed and the hosted file stays.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

PROTECTED_NAMES = {"schedule.bin", "feed.json", "skill.json"}
SKIP_PARTS = {"__pycache__", ".git", "data"}


def dependency_lines(text: str) -> list[str]:
    """Installable requirement lines, without comments or blanks."""
    found = []
    for raw in text.splitlines():
        body = raw.split("#", 1)[0].strip()
        if body:
            found.append(body)
    return found


def find_code_root(hosted: Path) -> Path:
    """Directory that holds the handler. Printed layout is the caller's job."""
    matches = []
    for path in hosted.rglob("lambda_function.py"):
        if not path.is_file() or ".git" in path.parts or "skill-package" in path.parts:
            continue
        matches.append(path.parent)
    if not matches:
        raise SystemExit(f"no lambda_function.py under {hosted}")
    matches = [path for path in matches if path != hosted]
    if not matches:
        raise SystemExit(f"lambda_function.py is at the root of {hosted}; refusing to sync the whole clone")
    if len(matches) == 1:
        return matches[0]
    named = [path for path in matches if path.name == "lambda"]
    if len(named) == 1:
        return named[0]
    listed = ", ".join(str(path) for path in matches)
    raise SystemExit(f"more than one lambda_function.py under {hosted}: {listed}")


def print_layout(hosted: Path) -> None:
    print("Hosted repository layout:")
    for path in sorted(hosted.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(hosted)
        if path.is_dir():
            print(f"  {rel}/")
        elif path.stat().st_size > 200_000:
            print(f"  {rel} ({path.stat().st_size} bytes)")
        else:
            print(f"  {rel}")


def _source_files(source: Path) -> dict[Path, Path]:
    files = {}
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source)
        if set(rel.parts) & SKIP_PARTS or path.suffix == ".pyc":
            continue
        if path.name in PROTECTED_NAMES:
            continue
        files[rel] = path
    return files


def _same_bytes(left: Path, right: Path) -> bool:
    return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()


def _show_diff(rel: Path, old: str, new: str) -> None:
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"hosted/{rel}",
        tofile=f"lambda/{rel}",
    )
    print("".join(diff), end="" if old.endswith("\n") or not old else "\n")


def sync(source: Path, hosted: Path, dry_run: bool) -> list[str]:
    """Apply source onto the hosted code root. Return the relative paths that change."""
    if not source.is_dir():
        raise SystemExit(f"missing source {source}")
    print_layout(hosted)
    code_root = find_code_root(hosted)
    rel_root = code_root.relative_to(hosted)
    print(f"code_root={rel_root.as_posix()}")
    source_files = _source_files(source)
    changed: list[str] = []

    requirements = Path("requirements.txt")
    if requirements in source_files:
        dest = code_root / requirements
        new_text = source_files[requirements].read_text(encoding="utf-8")
        old_text = dest.read_text(encoding="utf-8") if dest.is_file() else ""
        if dependency_lines(old_text) != dependency_lines(new_text):
            print(
                "requirements.txt dependency lines differ. "
                "Keeping the hosted file. The hosted runtime already provides boto3; "
                "this job does not add or remove dependencies."
            )
            _show_diff(rel_root / requirements, old_text, new_text)
        elif not _same_bytes(source_files[requirements], dest):
            changed.append(str(rel_root / requirements))
            if dry_run:
                print(f"would update {rel_root / requirements}")
            else:
                dest.write_text(new_text, encoding="utf-8")
        del source_files[requirements]

    for rel, path in sorted(source_files.items()):
        dest = code_root / rel
        if _same_bytes(path, dest):
            continue
        changed.append(str(rel_root / rel))
        if dry_run:
            print(f"would update {rel_root / rel}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())

    for path in sorted(code_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(code_root)
        if "skill-package" in path.parts:
            continue
        if set(rel.parts) & SKIP_PARTS or path.name in PROTECTED_NAMES:
            continue
        if rel == requirements or rel in source_files:
            continue
        if path.suffix != ".py":
            continue
        changed.append(f"delete {rel_root / rel}")
        if dry_run:
            print(f"would delete {rel_root / rel}")
        else:
            path.unlink()

    if changed:
        print("changes:")
        for item in changed:
            print(f"  {item}")
    else:
        print("lambda code already matches")
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosted", type=Path, help="Clone of the Alexa-hosted repository")
    parser.add_argument("--source", type=Path, help="lambda/ directory (default: this repo)")
    parser.add_argument("--dry-run", action="store_true", help="Print the diff and write nothing")
    parser.add_argument("--print-code-root", action="store_true", help="Print the code directory and exit")
    args = parser.parse_args()
    hosted = args.hosted.resolve()
    if args.print_code_root:
        print_layout(hosted)
        print(find_code_root(hosted).relative_to(hosted).as_posix())
        return
    source = args.source or Path(__file__).resolve().parents[1] / "lambda"
    sync(source, hosted, args.dry_run)


if __name__ == "__main__":
    main()
