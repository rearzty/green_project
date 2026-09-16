"""Convert DWG -> DXF, since ezdxf cannot read DWG directly (it's a closed
Autodesk binary format).

Two interchangeable backends, because neither is bundled with the project:

* **LibreDWG** (`dwg2dxf`) — GNU project, GPL-3.0, installed as a normal CLI
  tool. Verified against the pilot dataset: converts every DWG version present
  there (AC1021/R2007, AC1027/R2013, AC1032/R2018), including the drawings the
  project reads utilities and dendroplans out of.
  Availability, checked rather than assumed: `brew install libredwg` works on
  macOS (0.14 bottled). There is **no** `libredwg-tools` package in Debian
  trixie — the backend image's base — and no PyPI distribution, so the image
  has to build it. **Use 0.14 or newer, and pin it.** 0.13.3 converts the pilot
  drawings without complaint and silently writes a malformed DXF for the main
  one — a long MTEXT note breaks across lines and desynchronizes the group-code
  stream, so `ezdxf` aborts a 380k-line file at line 382545. Same input through
  0.14: all 30 files convert and parse. The failure mode is silent corruption,
  not an error, which is the reason to pin rather than take whatever is around.
  The build needs `pkg-config` (0.14 checks for it, 0.13 did not) and one flag,
  because the release compiles with `-Werror` and Debian's current GCC rejects
  it on `-Walloc-size`:

      apt-get install -y build-essential wget pkg-config libpcre2-dev
      wget https://ftp.gnu.org/gnu/libredwg/libredwg-0.14.tar.xz
      tar xf libredwg-0.14.tar.xz && cd libredwg-0.14
      ./configure --disable-bindings --disable-shared --disable-werror \
                  CFLAGS="-O2 -Wno-error"
      make -j4 && make install && ldconfig

  This is what `infra/Dockerfile.backend` does, in a separate `libredwg` stage
  so the multi-minute compile caches independently of the repo. The binary
  builds static (`--disable-shared`) and links only against libc/libm, so the
  runtime image takes it as a single file with no extra packages. LibreDWG is
  GPL-3.0 and is invoked here as a separate process, never linked.
* **ODA File Converter** — the official free Open Design Alliance tool,
  https://www.opendesign.com/guestfiles/oda_file_converter. Installed by hand,
  GUI-oriented, but the reference implementation if LibreDWG ever mangles a
  drawing.

`convert_dwg_to_dxf()` picks whichever is on PATH unless told otherwise. If
neither is, it raises a clear error rather than failing obscurely deep in ezdxf.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

Backend = Literal["auto", "libredwg", "oda"]

LIBREDWG_CANDIDATES = ["dwg2dxf"]
ODA_CONVERTER_CANDIDATES = ["ODAFileConverter", "ODAFileConverter.exe"]


class DwgConverterNotFoundError(RuntimeError):
    pass


# Kept under the old name too: it was the only failure mode this module had, and
# callers/tests may still catch it by that name.
ODAConverterNotFoundError = DwgConverterNotFoundError


def _which(candidates: list[str]) -> str | None:
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _find_oda_converter() -> str:
    found = _which(ODA_CONVERTER_CANDIDATES)
    if found:
        return found
    raise DwgConverterNotFoundError(
        "ODA File Converter not found on PATH. Install it from "
        "https://www.opendesign.com/guestfiles/oda_file_converter and ensure "
        "the executable is on PATH, or pass its path explicitly."
    )


def available_backend() -> Backend | None:
    """Which converter is usable right now, preferring the installable one."""
    if _which(LIBREDWG_CANDIDATES):
        return "libredwg"
    if _which(ODA_CONVERTER_CANDIDATES):
        return "oda"
    return None


def convert_with_libredwg(dwg_path: str | Path, output_dir: str | Path, executable: str | None = None) -> Path:
    """Convert one DWG with LibreDWG's `dwg2dxf`.

    Unlike the ODA converter this is a plain file-in/file-out CLI, so there is
    no directory dance and no chance of picking up a sibling drawing.
    """
    dwg_path = Path(dwg_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    executable = executable or _which(LIBREDWG_CANDIDATES)
    if executable is None:
        raise DwgConverterNotFoundError(
            "LibreDWG's dwg2dxf not found on PATH. Install it with "
            "`brew install libredwg`, or build it from source "
            "(https://www.gnu.org/software/libredwg/) — it is not packaged for Debian."
        )

    output_path = output_dir / (dwg_path.stem + ".dxf")
    result = subprocess.run(
        [executable, "-o", str(output_path), str(dwg_path)],
        capture_output=True,
        text=True,
        # dwg2dxf echoes paths and embedded strings straight from the drawing,
        # which are not UTF-8 in files written by Russian-locale AutoCAD. With
        # strict decoding the wrapper dies on the *diagnostics* of a file it was
        # about to reject anyway — hit live on Xrefs/Освещение.dwg, where a
        # legitimate "READ ERROR" surfaced as UnicodeDecodeError instead.
        errors="replace",
    )
    # dwg2dxf reports partial-support warnings on stderr and still writes a
    # usable file, so the produced file — not the exit code — is the contract.
    if not output_path.exists():
        raise RuntimeError(
            f"dwg2dxf did not produce {output_path}. "
            f"exit={result.returncode} stderr={result.stderr.strip()[:500]}"
        )
    return output_path


def convert_with_oda(
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


def convert_dwg_to_dxf(
    dwg_path: str | Path,
    output_dir: str | Path,
    backend: Backend = "auto",
    oda_executable: str | None = None,
    output_version: str = "ACAD2018",
) -> Path:
    """Convert one DWG to DXF with whichever backend is available."""
    if backend == "auto":
        if oda_executable is not None:
            backend = "oda"
        else:
            resolved = available_backend()
            if resolved is None:
                raise DwgConverterNotFoundError(
                    "No DWG converter on PATH. Install LibreDWG "
                    "(`brew install libredwg`, or build from source) "
                    "or the ODA File Converter."
                )
            backend = resolved

    if backend == "libredwg":
        return convert_with_libredwg(dwg_path, output_dir)
    if backend == "oda":
        return convert_with_oda(dwg_path, output_dir, oda_executable, output_version)
    raise ValueError(f"Unknown DWG conversion backend: {backend!r}")
