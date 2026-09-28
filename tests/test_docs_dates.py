import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
FIXED = re.compile(r"\b(after|before|at)=(\"\d{4}-|datetime\(\d{4})")


def test_samples_use_relative_windows():
    # a fixed date falls out of the free 7 day window
    files = [ROOT / "README.md", *(ROOT / "examples").glob("*.py")]
    hits = [f"{f.name}: {m.group(0)}" for f in files for m in FIXED.finditer(f.read_text())]
    assert not hits, hits
