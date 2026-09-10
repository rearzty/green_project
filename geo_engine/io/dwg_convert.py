"""Convert DWG -> DXF via the ODA File Converter CLI, since ezdxf cannot read
DWG directly (it's a closed Autodesk binary format).

Requires the ODA File Converter to be installed separately:
https://www.opendesign.com/guestfiles/oda_file_converter (free, official).
This module only shells out to it — it is NOT bundled with the project and
is not installed by pip/docker automatically. If it's missing, we raise a
clear error rather than failing obscurely deep in ezdxf.

TODO: verify against a real Mosgeotrest DWG sample once available (15.09).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ODA_CONVERTER_CANDIDATES = [
    "ODAFileConverter",
    "ODAFileConverter.exe",
]


class ODAConverterNotFoundError(RuntimeError):
    pass


def _find_oda_converter() -> str:
    for candidate in ODA_CONVERTER_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    raise ODAConverterNotFoundError(
        "ODA File Converter not found on PATH. Install it from "
        "https://www.opendesign.com/guestfiles/oda_file_converter and ensure "
        "the executable is on PATH, or pass its path explicitly."
    )


def convert_dwg_to_dxf(
    dwg_path: str | Path,
    output_dir: str | Path,
    oda_executable: str | None = None,
    output_version: str = "ACAD2018",
) -> Path:
    """Convert a single DWG file to DXF using the ODA File Converter CLI.

    ODA File Converter operates on directories, not single files, so we point
    it at the parent directory of `dwg_path` with a wildcard filter and only
    return the one output file we expect.
    """
    dwg_path = Path(dwg_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    executable = oda_executable or _find_oda_converter()

    # ODAFileConverter <in_dir> <out_dir> <out_version> <out_type> <recurse> <audit> [filter]
    cmd = [
        executable,
        str(dwg_path.parent),
        str(output_dir),
        output_version,
        "DXF",
        "0",  # recurse subdirectories: no
        "1",  # audit each file: yes
        dwg_path.name,
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    expected_output = output_dir / (dwg_path.stem + ".dxf")
    if not expected_output.exists():
        raise RuntimeError(f"ODA File Converter did not produce expected output: {expected_output}")
    return expected_output
