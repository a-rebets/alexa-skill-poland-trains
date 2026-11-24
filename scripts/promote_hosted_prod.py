#!/usr/bin/env python3
"""Promote the Alexa-hosted master branch to prod with a file-scope guard.

Merges master into prod (equivalent to clicking "Promote to live" in the Alexa
Developer Console) and pushes origin HEAD:prod so Amazon updates the live
Lambda after its automated security checks.

Safety guards refuse to promote when:
  - --scope data   (weekly timetable refresh): anything other than
                   <code_root>/data/schedule.bin or <code_root>/data/feed.json
                   differs between origin/prod and master.
  - --scope lambda (skill code deploy): anything outside <code_root>/
                   (for example skill-package/) differs between origin/prod
                   and master.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from sync_hosted_lambda import find_code_root  # noqa: E402

AUTHOR_ARGS = ("-c", "user.name=Artem", "-c", "user.email=artem.rebets@gmail.com")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail}")
    return result


def _rev_parse(repo: Path, ref: str) -> str | None:
    result = _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _diff_names(repo: Path, left: str, right: str) -> list[str]:
    result = _git(repo, "diff", "--name-only", left, right)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _allowed_path(path: str, code_root: str, scope: str) -> bool:
    if scope == "data":
        return path in {f"{code_root}/data/schedule.bin", f"{code_root}/data/feed.json"}
    if scope == "lambda":
        return path.startswith(f"{code_root}/")
    raise ValueError(f"unknown scope: {scope}")


def _check_allowed(changed: list[str], code_root: str, scope: str) -> None:
    disallowed = [path for path in changed if not _allowed_path(path, code_root, scope)]
    if not disallowed:
        return
    print(
        f"::error::Refusing to promote master to prod: changes outside scope '{scope}':",
        file=sys.stderr,
    )
    for path in disallowed:
        print(f"  {path}", file=sys.stderr)
    if any(path.startswith("skill-package/") for path in disallowed):
        print(
            "skill-package/ changes require certification or manual review before promoting to prod.",
            file=sys.stderr,
        )
    elif scope == "data":
        print(
            "master has unpromoted code changes relative to prod. "
            "Promote or revert them before running the timetable refresh.",
            file=sys.stderr,
        )
    raise SystemExit(1)


def _write_output(output_path: Path | None, pushed: bool, commit: str) -> None:
    if output_path is None:
        return
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(f"pushed={'true' if pushed else 'false'}\n")
        handle.write(f"commit={commit}\n")


def promote(
    hosted: Path,
    scope: str,
    dry_run: bool = False,
    github_output: Path | None = None,
) -> tuple[bool, str]:
    """Merge master into prod and push origin HEAD:prod if allowed and needed."""
    hosted = hosted.resolve()
    rel_root = find_code_root(hosted).relative_to(hosted).as_posix()

    _git(hosted, "fetch", "--quiet", "origin", "prod")
    prod_sha = _rev_parse(hosted, "refs/remotes/origin/prod")
    if not prod_sha:
        raise SystemExit(
            "::error::Remote branch origin/prod was not found. Is the skill published to live?"
        )
    master_sha = _rev_parse(hosted, "refs/heads/master")
    if not master_sha:
        raise SystemExit("::error::Local branch master was not found in hosted clone.")

    if prod_sha == master_sha:
        print(f"Hosted prod already matches master ({prod_sha[:7]}).")
        _write_output(github_output, False, prod_sha)
        return False, prod_sha

    changed = _diff_names(hosted, "refs/remotes/origin/prod", "refs/heads/master")
    if not changed:
        print(
            f"Hosted prod tree already matches master ({prod_sha[:7]} vs {master_sha[:7]}); "
            "nothing to promote."
        )
        _write_output(github_output, False, prod_sha)
        return False, prod_sha

    _check_allowed(changed, rel_root, scope)
    print(f"Promoting master ({master_sha[:7]}) -> prod ({prod_sha[:7]}), files:")
    for path in changed:
        print(f"  {path}")

    if dry_run:
        print("Dry run: prod was not merged or pushed.")
        _write_output(github_output, False, prod_sha)
        return False, prod_sha

    _git(hosted, "checkout", "--quiet", "-B", "prod", "refs/remotes/origin/prod")
    merge = _git(hosted, *AUTHOR_ARGS, "merge", "--quiet", "--no-edit", "refs/heads/master", check=False)
    if merge.returncode != 0:
        _git(hosted, "merge", "--abort", check=False)
        _git(hosted, "checkout", "--quiet", "master", check=False)
        detail = (merge.stderr or merge.stdout).strip()
        raise SystemExit(
            f"::error::Could not merge master into prod cleanly: {detail}"
        )

    post_changed = _diff_names(hosted, "refs/remotes/origin/prod", "HEAD")
    try:
        _check_allowed(post_changed, rel_root, scope)
    except SystemExit:
        _git(hosted, "checkout", "--quiet", "master", check=False)
        raise

    pushed_sha = _rev_parse(hosted, "HEAD") or master_sha
    push = _git(hosted, "push", "origin", "HEAD:prod", check=False)
    if push.stdout:
        print(push.stdout, end="")
    if push.stderr:
        print(push.stderr, end="", file=sys.stderr)
    _git(hosted, "checkout", "--quiet", "master", check=False)
    if push.returncode != 0:
        raise SystemExit(f"::error::git push origin HEAD:prod failed (exit {push.returncode})")

    print(f"Promoted {pushed_sha[:7]} to prod.")
    _write_output(github_output, True, pushed_sha)
    return True, pushed_sha


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosted", type=Path, help="Clone of the Alexa-hosted repository")
    parser.add_argument(
        "--scope",
        choices=("data", "lambda"),
        required=True,
        help="Allowed file scope between origin/prod and master",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check guards and print diff without pushing")
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GITHUB_OUTPUT file to append pushed= and commit= to",
    )
    args = parser.parse_args(argv)
    promote(args.hosted, args.scope, dry_run=args.dry_run, github_output=args.github_output)


if __name__ == "__main__":
    main()
