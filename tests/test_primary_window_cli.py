"""End-to-end hashed-input multi-window CLI and offline isolation tests."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from backtesting.run_primary_backtest import main as single_main
from backtesting.run_primary_windows import load_window_specs, main
from tests.test_primary_cli import manifest_files


def window_file(tmp_path, data_manifest, windows=None):
    manifest = json.loads(Path(data_manifest).read_text())
    sessions = manifest["session_dates"]
    payload = {"schema_version": "primary_window_spec_v1", "windows": windows or [
        {"id": "first", "start": sessions[49], "end": sessions[51]},
        {"id": "second", "start": sessions[52], "end": sessions[55]},
    ]}
    path = tmp_path / "windows.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def arguments(manifest, windows, output):
    return ["--input", str(manifest), "--windows", str(windows), "--output", str(output),
            "--initial-cash", "1000", "--allocation", ".6"]


def test_valid_multi_window_cli_and_byte_determinism(tmp_path):
    manifest, _ = manifest_files(tmp_path)
    windows = window_file(tmp_path, manifest)
    one, two = tmp_path / "one.json", tmp_path / "two.json"
    assert main(arguments(manifest, windows, one)) == 0
    assert main(arguments(manifest, windows, two)) == 0
    assert one.read_bytes() == two.read_bytes()
    report = json.loads(one.read_text())
    assert report["schema_version"] == "primary_multi_window_v1"
    assert report["completed_window_count"] == 2
    assert report["valuation_mode"] == "verified_filing_inputs"
    assert report["window_definitions"][0]["boundary_policy"] == "inclusive; exact observed sessions required"
    assert str(tmp_path) not in one.read_text()


@pytest.mark.parametrize("bad", ["duplicate", "weekend", "reverse", "unknown", "empty", "nonfinite"])
def test_invalid_window_files(tmp_path, bad):
    manifest, _ = manifest_files(tmp_path)
    sessions = json.loads(manifest.read_text())["session_dates"]
    rows = [{"id": "one", "start": sessions[49], "end": sessions[51]}]
    payload = {"schema_version": "primary_window_spec_v1", "windows": rows}
    if bad == "duplicate":
        rows.append(dict(rows[0]))
    elif bad == "weekend":
        rows[0]["start"] = "2023-03-11"
    elif bad == "reverse":
        rows[0]["start"], rows[0]["end"] = rows[0]["end"], rows[0]["start"]
    elif bad == "unknown":
        rows[0]["extra"] = True
    elif bad == "empty":
        rows.clear()
    elif bad == "nonfinite":
        (tmp_path / "windows.json").write_text('{"schema_version":"primary_window_spec_v1","windows":NaN}')
    if bad != "nonfinite":
        (tmp_path / "windows.json").write_text(json.dumps(payload))
    output = tmp_path / "output.json"
    with pytest.raises(SystemExit):
        main(arguments(manifest, tmp_path / "windows.json", output))
    assert not output.exists()


def test_modified_hashed_input_existing_output_and_collisions_are_rejected(tmp_path):
    manifest, _ = manifest_files(tmp_path)
    windows = window_file(tmp_path, manifest)
    output = tmp_path / "report.json"
    output.write_text("preserve")
    with pytest.raises(SystemExit):
        main(arguments(manifest, windows, output))
    assert output.read_text() == "preserve"
    prices = tmp_path / "A.csv"
    prices.write_text(prices.read_text() + "\n")
    fresh = tmp_path / "fresh.json"
    with pytest.raises(SystemExit):
        main(arguments(manifest, windows, fresh))
    assert not fresh.exists()
    with pytest.raises(SystemExit):
        main(arguments(manifest, windows, windows))


def test_symlink_window_spec_and_manifest_are_rejected(tmp_path):
    manifest, _ = manifest_files(tmp_path)
    windows = window_file(tmp_path, manifest)
    linked_windows = tmp_path / "linked-windows.json"
    linked_windows.symlink_to(windows)
    with pytest.raises((ValueError, OSError), match="symlink"):
        load_window_specs(linked_windows)
    linked_manifest = tmp_path / "linked-input.json"
    linked_manifest.symlink_to(manifest)
    with pytest.raises(SystemExit):
        main(arguments(linked_manifest, windows, tmp_path / "out.json"))


def test_symlinked_manifest_reference_cannot_escape_manifest_directory(tmp_path):
    inside = tmp_path / "inside"
    inside.mkdir()
    manifest, _ = manifest_files(inside)
    windows = window_file(inside, manifest)
    price = inside / "A.csv"
    content = price.read_bytes()
    price.unlink()
    outside = tmp_path / "outside.csv"
    outside.write_bytes(content)
    price.symlink_to(outside)
    output = inside / "output.json"
    with pytest.raises(SystemExit):
        main(arguments(manifest, windows, output))
    assert not output.exists()


def test_single_window_cli_remains_unchanged(tmp_path):
    manifest, _ = manifest_files(tmp_path)
    output = tmp_path / "single.json"
    assert single_main(["--input", str(manifest), "--output", str(output),
                        "--initial-cash", "1000", "--allocation", ".6"]) == 0
    report = json.loads(output.read_text())
    assert report["schema_version"] == "primary_execution_coupled_v1"
    assert report["windows"] == []


def test_fresh_process_is_offline_and_provider_free(tmp_path):
    manifest, _ = manifest_files(tmp_path)
    windows = window_file(tmp_path, manifest)
    output = tmp_path / "offline.json"
    code = '''
import importlib.abc, socket, sys
class Deny(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('anthropic','alpaca','db','engine.valuation_engine','engine.price_action_engine.market_data')):
            raise AssertionError('External import: '+fullname)
sys.meta_path.insert(0,Deny())
def deny(*a,**k): raise AssertionError('Network access')
socket.create_connection=deny
socket.socket.connect=deny
from backtesting.run_primary_windows import main
main(sys.argv[1:])
'''
    result = subprocess.run([sys.executable, "-c", code, *arguments(manifest, windows, output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["completed_window_count"] == 2
