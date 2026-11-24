"""Tests for promoting master -> prod on the Alexa-hosted repo and live smoke checks."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


promote_hosted = _load_script("promote_hosted_prod")
smoke_test = _load_script("smoke_test")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_hosted_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare origin with master and prod at the same base commit, and a clone on master."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "checkout", "-b", "master")
    _write(seed / "lambda" / "lambda_function.py", "def lambda_handler(e, c): return {}\n")
    _write(seed / "lambda" / "data" / "schedule.bin", "v1-schedule")
    _write(seed / "lambda" / "data" / "feed.json", '{"feed_end":"2026-11-21"}\n')
    _write(seed / "skill-package" / "skill.json", '{"manifest":{}}\n')
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "push", "-u", "origin", "master")
    _git(seed, "push", "origin", "master:prod")

    hosted = tmp_path / "hosted"
    subprocess.run(
        ["git", "clone", "--branch", "master", str(origin), str(hosted)],
        check=True,
        capture_output=True,
    )
    _git(hosted, "config", "user.name", "Test")
    _git(hosted, "config", "user.email", "test@example.com")
    return origin, hosted


def _commit_on_master(hosted: Path, updates: dict[str, str], message: str = "update") -> str:
    for rel, content in updates.items():
        _write(hosted / rel, content)
    _git(hosted, "add", "-A")
    _git(hosted, "commit", "-m", message)
    _git(hosted, "push", "origin", "HEAD:master")
    return _git(hosted, "rev-parse", "HEAD")


def test_promote_data_scope_promotes_timetable_updates(tmp_path):
    origin, hosted = _make_hosted_repos(tmp_path)
    master_sha = _commit_on_master(
        hosted,
        {
            "lambda/data/schedule.bin": "v2-schedule",
            "lambda/data/feed.json": '{"feed_end":"2026-12-23"}\n',
        },
        "Timetable refresh",
    )
    gh_out = tmp_path / "github_output.txt"
    pushed, commit = promote_hosted.promote(hosted, scope="data", github_output=gh_out)

    assert pushed is True
    assert commit == master_sha
    assert _git(origin, "rev-parse", "refs/heads/prod") == master_sha
    assert _git(hosted, "rev-parse", "--abbrev-ref", "HEAD") == "master"
    out_text = gh_out.read_text(encoding="utf-8")
    assert "pushed=true\n" in out_text
    assert f"commit={master_sha}\n" in out_text


def test_promote_data_scope_refuses_unpromoted_code_changes(tmp_path, capsys):
    origin, hosted = _make_hosted_repos(tmp_path)
    prod_before = _git(origin, "rev-parse", "refs/heads/prod")
    _commit_on_master(
        hosted,
        {
            "lambda/data/schedule.bin": "v2-schedule",
            "lambda/lambda_function.py": "def lambda_handler(e, c): return {'wip': True}\n",
        },
        "Unpromoted console edit",
    )

    with pytest.raises(SystemExit):
        promote_hosted.promote(hosted, scope="data")

    assert _git(origin, "rev-parse", "refs/heads/prod") == prod_before
    err = capsys.readouterr().err
    assert "lambda/lambda_function.py" in err
    assert "unpromoted code changes" in err


def test_promote_lambda_scope_promotes_code_and_data(tmp_path):
    origin, hosted = _make_hosted_repos(tmp_path)
    master_sha = _commit_on_master(
        hosted,
        {
            "lambda/lambda_function.py": "def lambda_handler(e, c): return {'v': 2}\n",
            "lambda/data/schedule.bin": "v2-schedule",
            "lambda/data/feed.json": '{"feed_end":"2026-12-23"}\n',
        },
        "Deploy skill code",
    )
    pushed, commit = promote_hosted.promote(hosted, scope="lambda")

    assert pushed is True
    assert commit == master_sha
    assert _git(origin, "rev-parse", "refs/heads/prod") == master_sha


def test_promote_lambda_scope_refuses_skill_package_changes(tmp_path, capsys):
    origin, hosted = _make_hosted_repos(tmp_path)
    prod_before = _git(origin, "rev-parse", "refs/heads/prod")
    _commit_on_master(
        hosted,
        {
            "lambda/lambda_function.py": "def lambda_handler(e, c): return {'v': 2}\n",
            "skill-package/skill.json": '{"manifest":{"changed":true}}\n',
        },
        "Manifest edit on master",
    )

    with pytest.raises(SystemExit):
        promote_hosted.promote(hosted, scope="lambda")

    assert _git(origin, "rev-parse", "refs/heads/prod") == prod_before
    err = capsys.readouterr().err
    assert "skill-package/skill.json" in err
    assert "certification" in err


def test_promote_noop_when_prod_already_matches_master(tmp_path):
    origin, hosted = _make_hosted_repos(tmp_path)
    base = _git(origin, "rev-parse", "refs/heads/prod")

    pushed, commit = promote_hosted.promote(hosted, scope="data")
    assert pushed is False
    assert commit == base


def test_promote_noop_when_only_empty_retry_commits_exist(tmp_path):
    origin, hosted = _make_hosted_repos(tmp_path)
    prod_before = _git(origin, "rev-parse", "refs/heads/prod")
    _git(hosted, "commit", "--allow-empty", "-m", "Redeploy retry 1")
    _git(hosted, "push", "origin", "HEAD:master")

    pushed, commit = promote_hosted.promote(hosted, scope="data")
    assert pushed is False
    assert commit == prod_before
    assert _git(origin, "rev-parse", "refs/heads/prod") == prod_before


def test_promote_dry_run_does_not_push(tmp_path):
    origin, hosted = _make_hosted_repos(tmp_path)
    prod_before = _git(origin, "rev-parse", "refs/heads/prod")
    _commit_on_master(hosted, {"lambda/data/schedule.bin": "v2-schedule"})

    pushed, commit = promote_hosted.promote(hosted, scope="data", dry_run=True)
    assert pushed is False
    assert commit == prod_before
    assert _git(origin, "rev-parse", "refs/heads/prod") == prod_before


def test_smoke_test_write_includes_optional_date(tmp_path):
    without_date = tmp_path / "req-no-date.json"
    smoke_test.write(str(without_date))
    data = json.loads(without_date.read_text(encoding="utf-8"))
    slots = data["request"]["intent"]["slots"]
    assert slots["time"]["value"] == "08:00"
    assert "value" not in slots["date"]

    with_date = tmp_path / "req-date.json"
    smoke_test.write(str(with_date), "2026-12-23")
    data = json.loads(with_date.read_text(encoding="utf-8"))
    assert data["request"]["intent"]["slots"]["date"]["value"] == "2026-12-23"


def test_wait_live_smoke_polls_until_live_updates(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls.txt"
    fake_ask = bin_dir / "ask"
    fake_ask.write_text(
        f"""#!/usr/bin/env python3
import json, pathlib, sys
print("node deprecation warning on stderr", file=sys.stderr)
p = pathlib.Path({str(counter)!r})
count = int(p.read_text()) if p.exists() else 0
count += 1
p.write_text(str(count))
if count < 3:
    ssml = "<speak>The timetable isn't available yet.</speak>"
else:
    ssml = "<speak>Departures from eight am on December twenty-third. Zachodnia station. Towards Centralna: R one at eight oh five.</speak>"
print(json.dumps({{"status": "SUCCESSFUL", "result": {{"skillExecutionInfo": {{"invocationResponse": {{"body": {{"response": {{"outputSpeech": {{"ssml": ssml}}}}}}}}}}}}}}))
""",
        encoding="utf-8",
    )
    fake_ask.chmod(fake_ask.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKILL_ID"] = "amzn1.ask.skill.test"
    env["LIVE_SMOKE_ATTEMPTS"] = "5"
    env["LIVE_SMOKE_SLEEP"] = "0"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "wait_live_smoke.sh"), "true", "2026-12-23"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert int(counter.read_text(encoding="utf-8").strip()) == 3
    assert "smoke test passed" in result.stdout
    req = json.loads((tmp_path / "request-live.json").read_text(encoding="utf-8"))
    assert req["request"]["intent"]["slots"]["date"]["value"] == "2026-12-23"


def test_wait_live_smoke_fails_immediately_when_not_promoted(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "calls.txt"
    fake_ask = bin_dir / "ask"
    fake_ask.write_text(
        f"""#!/usr/bin/env python3
import json, pathlib
p = pathlib.Path({str(counter)!r})
count = int(p.read_text()) if p.exists() else 0
count += 1
p.write_text(str(count))
ssml = "<speak>The timetable isn't available yet.</speak>"
print(json.dumps({{"status": "SUCCESSFUL", "result": {{"skillExecutionInfo": {{"invocationResponse": {{"body": {{"response": {{"outputSpeech": {{"ssml": ssml}}}}}}}}}}}}}}))
""",
        encoding="utf-8",
    )
    fake_ask.chmod(fake_ask.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SKILL_ID"] = "amzn1.ask.skill.test"
    env["LIVE_SMOKE_ATTEMPTS"] = "5"
    env["LIVE_SMOKE_SLEEP"] = "0"

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "wait_live_smoke.sh"), "false", "2026-12-23"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert int(counter.read_text(encoding="utf-8").strip()) == 1


def test_workflows_promote_to_prod_and_smoke_test_live():
    deploy = (ROOT / ".github" / "workflows" / "deploy-skill.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github" / "workflows" / "refresh-timetable.yml").read_text(encoding="utf-8")

    assert "promote_hosted_prod.py hosted --scope lambda" in deploy
    assert "promote_hosted_prod.py hosted --scope data" in refresh
    assert "wait_live_smoke.sh" in deploy
    assert "wait_live_smoke.sh" in refresh
    assert "scripts/promote_hosted_prod.py" in deploy
    assert "scripts/wait_live_smoke.sh" in deploy
    assert "scripts/smoke_test.py" in deploy
