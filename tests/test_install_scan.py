"""`hermes plugins install owner/repo` runs Hermes' security scan; a caution verdict blocks a non-interactive
install and asks [y/N] in a terminal. The shipped tree must scan as safe."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_repository_scans_safe(tmp_path):
    guard = pytest.importorskip('tools.plugin_guard')
    files = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True).stdout
    for rel in filter(None, files.decode().split('\0')):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
    result = guard.scan_plugin(tmp_path, 'github:100yenadmin/glasses-platform')
    blocking = [(f.severity, f.pattern_id, f.file, f.line) for f in result.findings if f.severity in ('high', 'critical')]
    assert result.verdict == 'safe', blocking
