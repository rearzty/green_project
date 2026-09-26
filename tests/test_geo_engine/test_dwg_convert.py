"""DWG -> DXF conversion. Needs a converter on PATH, so it skips without one.

Separate from test_pilot_dataset.py on purpose: this is about the converter
wrapper, not about the dataset, and it builds its own inputs.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from geo_engine.io.dwg_convert import (
    LIBREDWG_CANDIDATES,
    DwgConverterNotFoundError,
    ODAConverterNotFoundError,
    convert_dwg_to_dxf,
    convert_with_libredwg,
    convert_with_oda,
)


def test_the_old_exception_name_still_catches_the_new_error():
    """`ODAConverterNotFoundError` was the module's only failure mode before a
    second backend existed. Callers catching it by name must keep working.
    """
    assert ODAConverterNotFoundError is DwgConverterNotFoundError


def test_unknown_backend_is_rejected_by_name(tmp_path):
    with pytest.raises(ValueError, match="backend"):
        convert_dwg_to_dxf(tmp_path / "x.dwg", tmp_path, backend="autocad")


@pytest.mark.skipif(
    not any(shutil.which(candidate) for candidate in LIBREDWG_CANDIDATES),
    reason="LibreDWG not on PATH",
)
def test_a_file_dwg2dxf_cannot_read_raises_a_runtime_error(tmp_path):
    """Regression for a real crash: dwg2dxf echoes paths and strings straight
    out of the drawing, and in files written by Russian-locale AutoCAD those are
    not UTF-8. Decoding its output strictly killed the wrapper on the
    *diagnostics* of a file it was correctly refusing — a UnicodeDecodeError
    where the caller expected a normal "this file did not convert". Hit live on
    the pilot street's Xrefs/Освещение.dwg.

    Gated on LibreDWG's own presence, not on `available_backend()`'s pick —
    since ODA became the preferred backend, a machine with both installed
    would otherwise skip this LibreDWG-specific regression forever, even
    though the function under test (`convert_with_libredwg`) is called
    directly here and doesn't care which backend is "preferred".
    """
    broken = tmp_path / "Освещение.dwg"
    broken.write_bytes(b"AC1032" + b"\x00\x9c\xd1\x82" * 64)

    with pytest.raises(RuntimeError) as excinfo:
        convert_with_libredwg(broken, tmp_path / "out")

    assert "dwg2dxf did not produce" in str(excinfo.value)


class TestOdaFilenameIsolation:
    """Live hang, "10. Старый Гай ул": a real bureau filename delivered as
    part of the pilot dataset, `output[1-8]_3_ДЖКХ-24_02797kl.dwg` (the
    brackets are a survey-batch id, not a glob), passed straight through as
    ODA File Converter's own file-filter argument -- confirmed directly, not
    guessed: the identical file and directory converts in seconds once the
    filter is `*.dwg` instead of the literal name, so ODA (or the Qt
    wildcard engine underneath it) was evidently trying to match `[1-8]` as
    a character class against a name that only contains it literally, and
    never resolved -- no error, no timeout, indefinite hang. Under
    `resolve_bundle_inputs`'s `ThreadPoolExecutor` (up to 8 concurrent
    conversions) this stalled the whole bundle read, not just one file.

    These check the fix's actual mechanism -- what `convert_with_oda` hands
    to the subprocess -- via a mocked `subprocess.run`, not a real ODA
    invocation: the point is to catch a regression here without depending on
    this machine having a real ODA/Qt/X11 stack (not guaranteed in CI), and
    without a test that could itself hang if the bug ever came back.
    """

    def _fake_run(self, expected_output: Path, captured: dict):
        def run(cmd, **kwargs):
            captured["cmd"] = cmd
            expected_output.parent.mkdir(parents=True, exist_ok=True)
            expected_output.write_text("fake dxf")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        return run

    def test_the_filter_argument_is_always_a_plain_wildcard_not_the_filename(self, tmp_path):
        source = tmp_path / "output[1-8]_3_ДЖКХ-24_02797kl.dwg"
        source.write_bytes(b"fake dwg content")
        output_dir = tmp_path / "out"
        expected_output = output_dir / "output[1-8]_3_ДЖКХ-24_02797kl.dxf"
        captured: dict = {}

        with patch("geo_engine.io.dwg_convert.subprocess.run", side_effect=self._fake_run(expected_output, captured)):
            result = convert_with_oda(source, output_dir, oda_executable="fake-oda")

        assert result == expected_output
        filter_arg = captured["cmd"][-1]
        assert filter_arg == "*.dwg"
        assert "[1-8]" not in filter_arg

    def test_the_isolated_input_directory_holds_only_this_one_file(self, tmp_path):
        """A directory-level filter (`*.dwg`) would otherwise also matter if
        two convert_with_oda calls could share an input directory with
        several real files in it -- proving the isolated directory holds
        only the one file being converted is what makes the plain wildcard
        filter safe to use at all, not just a way to dodge one bad filename.
        """
        source = tmp_path / "a.dwg"
        source.write_bytes(b"fake dwg content")
        (tmp_path / "b.dwg").write_bytes(b"a sibling file that must not be swept in")
        output_dir = tmp_path / "out"
        expected_output = output_dir / "a.dxf"
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            # The isolated dir only exists for the duration of the real
            # `tempfile.TemporaryDirectory` `with` block inside
            # convert_with_oda -- it is torn down before this function
            # returns, so its contents have to be captured here, from
            # inside the mocked subprocess call, not after the fact.
            isolated_dir = Path(cmd[-7])
            captured["isolated_dir"] = isolated_dir
            captured["contents"] = sorted(p.name for p in isolated_dir.iterdir())
            expected_output.parent.mkdir(parents=True, exist_ok=True)
            expected_output.write_text("fake dxf")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("geo_engine.io.dwg_convert.subprocess.run", side_effect=fake_run):
            convert_with_oda(source, output_dir, oda_executable="fake-oda")

        assert captured["isolated_dir"] != source.parent
        assert captured["contents"] == ["a.dwg"]
