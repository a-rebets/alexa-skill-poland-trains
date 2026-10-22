import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

SAMPLE_GTFS = ROOT / "tests" / "fixtures" / "sample_gtfs"


def _zip_sample(dest: Path) -> None:
    with zipfile.ZipFile(dest, "w") as archive:
        for path in sorted(SAMPLE_GTFS.glob("*.txt")):
            archive.write(path, path.name)


@pytest.fixture(scope="session")
def sample_feed(tmp_path_factory):
    """Build schedule.bin and the picker data files from the committed GTFS sample.

    The weekly Action builds the live timetable. Tests do not read that output,
    and the repository does not store it.
    """
    work = tmp_path_factory.mktemp("sample-feed")
    zip_path = work / "sample.zip"
    _zip_sample(zip_path)
    out = work / "schedule.bin"
    picker = work / "picker"
    subprocess.check_call(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_data.py"),
            "--zip",
            str(zip_path),
            "--out",
            str(out),
            "--picker-dir",
            str(picker),
        ],
        cwd=ROOT,
    )
    return {"schedule": out, "picker": picker}
