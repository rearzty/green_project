"""Convert DWG -> DXF, since ezdxf cannot read DWG directly (it's a closed
Autodesk binary format).

Two interchangeable backends, because neither is bundled with the project:

* **ODA File Converter** — the official free Open Design Alliance tool,
  https://www.opendesign.com/guestfiles/oda_file_converter, now the **primary**
  backend. It was not always: ODA's own FAQ describes the general download as
  free for non-commercial use only, and an earlier pass through this decision
  (before this project had asked anyone) took that at face value and rejected
  ODA outright over the licensing risk. That conclusion is now superseded, not
  merely re-guessed — the organizers were asked directly and answered in
  writing: **«Использовать можно, в рамках ТЗ ограничений на такой инструмент
  нет»** (usable, no restriction on this tool within the brief). What tipped
  the switch technically, not just the license clearing: LibreDWG cannot
  recover REGION/3DSOLID entities at all — verified directly, its conversion
  writes them with a 0-byte ACIS payload (`acis_data`/`sab`/`sat` all empty),
  so the geometry is gone before `dxf_reader.py` ever sees the file. Real
  bureau drawings use REGION for surface fills that matter to this project
  (`"2. Песчаный переулок"`'s topography/utility xrefs: existing greenery, gas
  mains, cables, street boundaries — 189+ entities on one sheet alone). ODA
  preserves the real binary payload (confirmed: `acis_data` starts `b"ASM "`,
  real ShapeManager bytes, where the same entity through LibreDWG was empty),
  which `geo_engine/io/dxf_reader.py::_region_to_geometry()` then turns into
  real polygon geometry via `ezdxf.acis.api`. Ships as a Qt6 GUI application
  (CLI-capable but still needs a working, if virtual, X server) — `xvfb-run`
  wraps every invocation below, see `convert_with_oda()`. Installed via
  `infra/Dockerfile.backend`'s own apt stage (`.deb` fetched directly from
  opendesign.com's own guestfiles route, confirmed to be a stable,
  unauthenticated download, not a short-lived presigned link), not by hand.
* **LibreDWG** (`dwg2dxf`) — GNU project, GPL-3.0, kept as the fallback when
  ODA is unavailable (e.g. a bare-metal dev machine without the Qt/X11
  runtime stack ODA needs). Verified against the pilot dataset: converts
  every DWG version present there (AC1021/R2007, AC1027/R2013, AC1032/R2018),
  including the drawings the project reads utilities and dendroplans out of
  — it is a perfectly good converter for everything except ACIS solids/faces.
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

`convert_dwg_to_dxf()` picks whichever is on PATH unless told otherwise,
preferring ODA. If neither is, it raises a clear error rather than failing
obscurely deep in ezdxf.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

Backend = Literal["auto", "libredwg", "oda"]

LIBREDWG_CANDIDATES = ["dwg2dxf"]
ODA_CONVERTER_CANDIDATES = ["ODAFileConverter", "ODAFileConverter.exe"]
XVFB_RUN_CANDIDATES = ["xvfb-run"]


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
    """Which converter is usable right now, preferring ODA -- see this
    module's docstring for why (REGION/3DSOLID recovery, organizer sign-off)."""
    if _which(ODA_CONVERTER_CANDIDATES):
        return "oda"
    if _which(LIBREDWG_CANDIDATES):
        return "libredwg"
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
    it at an isolated, throwaway directory holding a *copy* of just this one
    file, filtered by the unconditionally-safe `*.dwg` -- not at
    `dwg_path.parent` filtered by `dwg_path.name`, which was the original
    approach here and hung for 300+s on a real pilot filename: "10. Старый
    Гай ул" delivers utility sheets named by survey-batch id, literally
    `output[1-8]_3_ДЖКХ-24_02797kl.dwg` (the brackets are the batch id, not a
    glob). ODA's own file-filter argument evidently runs that name through a
    wildcard/regex engine (Qt's, most likely, though this is inferred from
    behaviour, not confirmed from ODA's source) that tries to match `[1-8]`
    as a character class against a name that only contains it literally, and
    never resolves -- confirmed directly, not guessed: the exact same
    directory and file converts in well under a minute once the filter is
    changed to plain `*.dwg`. Since the only defence against an unknown set
    of future glob metacharacters in a real filename is to never build a
    filter out of one, every call gets its own private input directory
    instead of trying to escape the name.

    It is a Qt6 GUI application under the hood -- even its command-line mode
    opens a (normally invisible) window and refuses to start without a
    working X server, `qt.qpa.xcb: could not connect to display`. Wrapped in
    `xvfb-run -a` when that's on PATH (true inside `infra/Dockerfile.backend`,
    which installs both), which starts a throwaway virtual display, picks a
    free display number itself (`-a`, safe for concurrent calls), and tears
    it down after. `XDG_RUNTIME_DIR` gets its own throwaway directory per
    call, for the same reason as the isolated input directory above --
    `dxf_reader.py::resolve_bundle_inputs()` converts a bundle's files
    through a `ThreadPoolExecutor` of up to 8 workers, so several of these
    run genuinely concurrently, and a directory shared across calls (the
    original approach) is exactly the kind of state concurrent invocations
    of the same external GUI toolkit have no business sharing, confirmed-safe
    or not. Where `xvfb-run` isn't on PATH (a native macOS/Windows install,
    which has a real display already), the command runs unwrapped, same as
    before.
    """
    dwg_path = Path(dwg_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    executable = oda_executable or _find_oda_converter()

    with tempfile.TemporaryDirectory(prefix="oda_in_") as isolated_dir_str, tempfile.TemporaryDirectory(
        prefix="oda_xdg_"
    ) as xdg_runtime_dir:
        isolated_dir = Path(isolated_dir_str)
        shutil.copy2(dwg_path, isolated_dir / dwg_path.name)

        # ODAFileConverter <in_dir> <out_dir> <out_version> <out_type> <recurse> <audit> [filter]
        cmd = [
            executable,
            str(isolated_dir),
            str(output_dir),
            output_version,
            "DXF",
            "0",  # recurse subdirectories: no
            "1",  # audit each file: yes
            "*.dwg",
        ]

        xvfb_run = _which(XVFB_RUN_CANDIDATES)
        if xvfb_run:
            cmd = [xvfb_run, "-a", *cmd]

        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = xdg_runtime_dir
        os.chmod(xdg_runtime_dir, 0o700)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Same reasoning as convert_with_libredwg: ODA echoes drawing paths
            # and diagnostics straight from the file, not guaranteed UTF-8 for a
            # Russian-locale AutoCAD export.
            errors="replace",
            env=env,
        )

    expected_output = output_dir / (dwg_path.stem + ".dxf")
    # Exit code, not the contract -- same reasoning as convert_with_libredwg's
    # dwg2dxf: ODA can print audit warnings and still produce a usable file.
    if not expected_output.exists():
        raise RuntimeError(
            f"ODA File Converter did not produce {expected_output}. "
            f"exit={result.returncode} stderr={result.stderr.strip()[:500]}"
        )
    return expected_output


def convert_with_oda_batch(
    dwg_paths: list[str | Path],
    output_dir: str | Path,
    oda_executable: str | None = None,
    output_version: str = "ACAD2018",
) -> dict[Path, Path]:
    """Convert several DWG files with ONE ODA File Converter invocation.

    ODA is a Qt6 GUI application even in its CLI mode, so every call to
    `convert_with_oda()` pays a fixed xvfb+Qt-startup cost before it touches a
    single byte of the actual drawing, on top of whatever the file itself
    costs to convert. A bundle with dozens of small xref files was paying
    that fixed cost once *per file*; batching pays it once for the whole
    group. Measured live on 8 real xref files from the pilot dataset
    (20-40 KB each): 8 sequential `convert_with_oda()` calls -- 3.91s total,
    0.49s/file average; one batched call over the same 8 -- 0.56s total,
    ~7x faster. On files this small the fixed per-call cost *is* essentially
    the whole 0.49s, not the conversion itself.

    Same isolation trick as `convert_with_oda()`, scaled up: every input file
    is copied into ONE throwaway directory (never the caller's own -- see
    that function's docstring on why a filter can never be built from a real
    filename) and given a unique numeric-prefixed name, since two different
    xref subfolders in a real bundle can legitimately share a basename (e.g.
    two different survey orders both naming a sheet `up.dwg`) -- copying them
    into one flat directory unrenamed would let the second silently overwrite
    the first before ODA ever runs.

    Returns a mapping from *original* path to its converted DXF for every
    input ODA actually produced output for. A file ODA could not convert
    (corruption, an unsupported feature) is simply absent from the returned
    dict, exactly like a single failed `convert_with_oda()` call today --
    one bad file in the batch does not take the rest of it down. If NOTHING
    in the batch produced output, that is not "every file happened to be
    bad" but almost certainly the converter itself failing to run at all
    (missing xvfb, wrong executable, ...), so that case raises with the
    captured stderr instead of silently returning an empty dict that would
    make every file in the batch look individually corrupt.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = oda_executable or _find_oda_converter()

    with tempfile.TemporaryDirectory(prefix="oda_in_") as isolated_dir_str, tempfile.TemporaryDirectory(
        prefix="oda_xdg_"
    ) as xdg_runtime_dir:
        isolated_dir = Path(isolated_dir_str)
        original_by_stem: dict[str, Path] = {}
        for index, raw_path in enumerate(dwg_paths):
            dwg_path = Path(raw_path)
            unique_stem = f"{index:04d}_{dwg_path.stem}"
            shutil.copy2(dwg_path, isolated_dir / f"{unique_stem}.dwg")
            original_by_stem[unique_stem] = dwg_path

        cmd = [
            executable,
            str(isolated_dir),
            str(output_dir),
            output_version,
            "DXF",
            "0",  # recurse subdirectories: no
            "1",  # audit each file: yes
            "*.dwg",
        ]

        xvfb_run = _which(XVFB_RUN_CANDIDATES)
        if xvfb_run:
            cmd = [xvfb_run, "-a", *cmd]

        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = xdg_runtime_dir
        os.chmod(xdg_runtime_dir, 0o700)

        result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env)

    results: dict[Path, Path] = {}
    for unique_stem, original_path in original_by_stem.items():
        expected_output = output_dir / f"{unique_stem}.dxf"
        if expected_output.exists():
            results[original_path] = expected_output

    if not results and dwg_paths:
        raise RuntimeError(
            f"ODA File Converter produced no output for any of {len(dwg_paths)} file(s). "
            f"exit={result.returncode} stderr={result.stderr.strip()[:500]}"
        )
    return results


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
