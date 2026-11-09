"""The plugin runs in Hermes's own plugin environment, which has only what pyproject.toml declares."""
import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_pairing_qr_dependency_is_declared():
    deps = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['dependencies']
    names = {d.split('>')[0].split('<')[0].split('=')[0].strip() for d in deps}
    assert {'aiohttp', 'qrcode'} <= names


def test_manifest_and_pyproject_versions_match():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    manifest = (ROOT / 'plugin.yaml').read_text()
    assert f'version: "{project}"' in manifest
