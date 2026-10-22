"""The hosted sync never touches the timetable or the live manifest."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_hosted_lambda", ROOT / "scripts" / "sync_hosted_lambda.py"
)
sync_hosted = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_hosted)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _hosted(tmp_path: Path) -> Path:
    hosted = tmp_path / "hosted"
    _write(hosted / "lambda" / "lambda_function.py", "print('old')\n")
    _write(hosted / "lambda" / "upcoming_trains" / "board.py", "OLD = 1\n")
    _write(hosted / "lambda" / "legacy.py", "STALE = True\n")
    _write(hosted / "lambda" / "requirements.txt", "boto3\n")
    _write(hosted / "lambda" / "data" / "schedule.bin", "timetable-bytes")
    _write(hosted / "lambda" / "data" / "feed.json", '{"feed_end":"2026-11-21"}\n')
    _write(hosted / "lambda" / "data" / "notes.py", "KEEP = True\n")
    _write(
        hosted / "skill-package" / "skill.json",
        '{"manifest":{"apis":{"custom":{"endpoint":{"uri":"arn:live"}}}}}\n',
    )
    return hosted


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "lambda"
    _write(source / "lambda_function.py", "print('new')\n")
    _write(source / "upcoming_trains" / "board.py", "OLD = 1\n")
    _write(source / "upcoming_trains" / "voice.py", "VOICE = 1\n")
    _write(source / "requirements.txt", "# no third-party packages\n")
    _write(source / "data" / "schedule.bin", "local-build-must-not-ship")
    _write(source / "data" / "feed.json", "{}\n")
    return source


def test_sync_updates_code_and_leaves_timetable_and_manifest(tmp_path, capsys):
    hosted = _hosted(tmp_path)
    source = _source(tmp_path)
    schedule = (hosted / "lambda" / "data" / "schedule.bin").read_bytes()
    feed = (hosted / "lambda" / "data" / "feed.json").read_bytes()
    manifest = (hosted / "skill-package" / "skill.json").read_bytes()
    requirements = (hosted / "lambda" / "requirements.txt").read_text(encoding="utf-8")

    changed = sync_hosted.sync(source, hosted, dry_run=False)
    captured = capsys.readouterr().out

    assert (hosted / "lambda" / "lambda_function.py").read_text(encoding="utf-8") == "print('new')\n"
    assert (hosted / "lambda" / "upcoming_trains" / "voice.py").is_file()
    assert not (hosted / "lambda" / "legacy.py").exists()
    assert (hosted / "lambda" / "data" / "schedule.bin").read_bytes() == schedule
    assert (hosted / "lambda" / "data" / "feed.json").read_bytes() == feed
    assert (hosted / "lambda" / "data" / "notes.py").read_text(encoding="utf-8") == "KEEP = True\n"
    assert (hosted / "skill-package" / "skill.json").read_bytes() == manifest
    assert (hosted / "lambda" / "requirements.txt").read_text(encoding="utf-8") == requirements
    assert "boto3" in captured
    assert "Keeping the hosted file" in captured
    assert "lambda/lambda_function.py" in changed
    assert "delete lambda/legacy.py" in changed
    assert not any("schedule.bin" in item or "feed.json" in item or "skill.json" in item for item in changed)


def test_dry_run_writes_nothing(tmp_path):
    hosted = _hosted(tmp_path)
    source = _source(tmp_path)
    before = {
        path.relative_to(hosted): path.read_bytes()
        for path in hosted.rglob("*")
        if path.is_file()
    }
    sync_hosted.sync(source, hosted, dry_run=True)
    after = {
        path.relative_to(hosted): path.read_bytes()
        for path in hosted.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_matching_requirements_comments_are_updated(tmp_path, capsys):
    hosted = _hosted(tmp_path)
    source = _source(tmp_path)
    _write(hosted / "lambda" / "requirements.txt", "# older comment\n")
    _write(source / "requirements.txt", "# no third-party packages\n")
    sync_hosted.sync(source, hosted, dry_run=False)
    assert (
        (hosted / "lambda" / "requirements.txt").read_text(encoding="utf-8")
        == "# no third-party packages\n"
    )
    assert "Keeping the hosted file" not in capsys.readouterr().out


def test_code_root_is_discovered_from_the_handler(tmp_path, capsys):
    hosted = _hosted(tmp_path)
    _write(hosted / "lambda" / "py" / "lambda_function.py", "print('nested')\n")
    # Two handlers: the directory named lambda wins.
    assert sync_hosted.find_code_root(hosted) == hosted / "lambda"
    sync_hosted.print_layout(hosted)
    assert "skill-package/" in capsys.readouterr().out


def test_handler_at_the_clone_root_is_refused(tmp_path):
    hosted = tmp_path / "hosted"
    _write(hosted / "lambda_function.py", "print('root')\n")
    with pytest.raises(SystemExit, match="root"):
        sync_hosted.find_code_root(hosted)


def test_deploy_workflow_does_not_run_on_pull_requests():
    deploy = (ROOT / ".github" / "workflows" / "deploy-skill.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github" / "workflows" / "refresh-timetable.yml").read_text(encoding="utf-8")
    header, _, _rest = deploy.partition("jobs:")
    assert "pull_request" not in header
    assert "branches: [main]" in header
    assert "lambda/**" in header
    assert "dry_run" in header
    assert "alexa-hosted-push" in deploy
    assert "alexa-hosted-push" in refresh
    assert "git push --force" not in deploy
    assert "git push --force" not in refresh
    assert "schedule\\.bin" in deploy
    assert "skill\\.json" in deploy
