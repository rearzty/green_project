"""DWG -> DXF conversion. Needs a converter on PATH, so it skips without one.

Separate from test_pilot_dataset.py on purpose: this is about the converter
wrapper, not about the dataset, and it builds its own inputs.
"""

import pytest

from geo_engine.io.dwg_convert import (
    DwgConverterNotFoundError,
    ODAConverterNotFoundError,
    available_backend,
    convert_dwg_to_dxf,
    convert_with_libredwg,
)


def test_the_old_exception_name_still_catches_the_new_error():
    """`ODAConverterNotFoundError` was the module's only failure mode before a
    second backend existed. Callers catching it by name must keep working.
    """
    assert ODAConverterNotFoundError is DwgConverterNotFoundError


def test_unknown_backend_is_rejected_by_name(tmp_path):
    with pytest.raises(ValueError, match="backend"):
        convert_dwg_to_dxf(tmp_path / "x.dwg", tmp_path, backend="autocad")


@pytest.mark.skipif(available_backend() != "libredwg", reason="LibreDWG not on PATH")
def test_a_file_dwg2dxf_cannot_read_raises_a_runtime_error(tmp_path):
    """Regression for a real crash: dwg2dxf echoes paths and strings straight
    out of the drawing, and in files written by Russian-locale AutoCAD those are
    not UTF-8. Decoding its output strictly killed the wrapper on the
    *diagnostics* of a file it was correctly refusing — a UnicodeDecodeError
    where the caller expected a normal "this file did not convert". Hit live on
    the pilot street's Xrefs/Освещение.dwg.
    """
    broken = tmp_path / "Освещение.dwg"
    broken.write_bytes(b"AC1032" + b"\x00\x9c\xd1\x82" * 64)

    with pytest.raises(RuntimeError) as excinfo:
        convert_with_libredwg(broken, tmp_path / "out")

    assert "dwg2dxf did not produce" in str(excinfo.value)
