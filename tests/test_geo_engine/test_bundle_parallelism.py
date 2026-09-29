"""Regression tests for this session's upload-speed work: worker-count sizing
derived from the machine's real core count instead of a hardcoded 8, greedy
LPT bin-packing for batched DWG conversion, and `resolve_and_read_bundle`'s
overlap of DWG->DXF conversion with DXF reading.

Real-world context for the numbers below is in docs/decision_log.md --
summarized here: the project brief's own guaranteed
minimum target hardware is 8 logical cores (ТЗ, "не менее 8 логических
ядер"), and a hardcoded worker cap of 8 happened to sit exactly on that
floor, silently leaving every core above it idle on a beefier machine
(measured live: 12-core dev container, both the DWG-conversion thread pool
and the DXF-read process pool topped out at ~800% CPU well short of 1200%).
"""

from unittest.mock import patch

import ezdxf
import pytest

from geo_engine.io.dxf_reader import (
    MOSGEOTREST_LAYER_MAP,
    _bundle_pool_worker_count,
    _lpt_partition,
    resolve_and_read_bundle,
)

GAS_LAYER = "Газопровод"


class TestBundlePoolWorkerCount:
    def test_stays_at_the_briefs_floor_on_an_exactly_eight_core_machine(self):
        """8 cores is the guaranteed minimum deployment target -- must not
        regress relative to the old hardcoded-8 behaviour there."""
        with patch("geo_engine.io.dxf_reader.os.cpu_count", return_value=8):
            assert _bundle_pool_worker_count(n_files=100) == 8

    def test_reserves_one_core_only_once_there_is_headroom_above_the_floor(self):
        with patch("geo_engine.io.dxf_reader.os.cpu_count", return_value=12):
            assert _bundle_pool_worker_count(n_files=100) == 11

    def test_never_exceeds_the_number_of_files(self):
        with patch("geo_engine.io.dxf_reader.os.cpu_count", return_value=12):
            assert _bundle_pool_worker_count(n_files=3) == 3

    def test_below_the_floor_still_uses_every_core_available(self):
        """Defensive, not an expected real case (the brief guarantees >= 8) --
        but a machine with fewer cores must not be starved further."""
        with patch("geo_engine.io.dxf_reader.os.cpu_count", return_value=4):
            assert _bundle_pool_worker_count(n_files=100) == 4

    def test_never_returns_zero(self):
        with patch("geo_engine.io.dxf_reader.os.cpu_count", return_value=8):
            assert _bundle_pool_worker_count(n_files=0) == 1


class TestLptPartition:
    def test_the_single_dominant_file_ends_up_alone_in_its_own_bin(self, tmp_path):
        """A flat size-descending sort only guarantees the biggest task
        starts first -- it says nothing about the rest. Greedy LPT must
        isolate the one huge file from the small ones so no bin ends up with
        both the giant AND a pile of small files."""
        big = tmp_path / "big.dwg"
        big.write_bytes(b"x" * 10_000)
        small_files = []
        for i in range(6):
            p = tmp_path / f"small_{i}.dwg"
            p.write_bytes(b"x" * 100)
            small_files.append(p)

        bins = _lpt_partition([big, *small_files], num_bins=3)

        assert len(bins) == 3
        big_bin = next(b for b in bins if big in b)
        assert len(big_bin) == 1, "the dominant file must not share a bin with anything else"
        other_bins = [b for b in bins if b is not big_bin]
        assert sum(len(b) for b in other_bins) == 6

    def test_fewer_files_than_bins_gives_one_file_per_bin_not_empty_bins(self, tmp_path):
        files = []
        for i in range(2):
            p = tmp_path / f"f_{i}.dwg"
            p.write_bytes(b"x" * 100)
            files.append(p)

        bins = _lpt_partition(files, num_bins=5)

        assert len(bins) == 2
        assert all(len(b) == 1 for b in bins)

    def test_a_single_bin_keeps_everything_together(self, tmp_path):
        files = []
        for i in range(4):
            p = tmp_path / f"f_{i}.dwg"
            p.write_bytes(b"x" * 100)
            files.append(p)

        assert _lpt_partition(files, num_bins=1) == [files]

    def test_empty_input_returns_no_bins(self):
        assert _lpt_partition([], num_bins=4) == []


class TestResolveAndReadBundleOverlap:
    """The whole point of resolve_and_read_bundle: a file's read starts as
    soon as ITS OWN conversion finishes, not after every file in the bundle
    has converted. That overlap must never leak into the returned order --
    result order has to stay canonical (main first, then bundle members in
    the same order resolve_bundle_inputs would produce them) regardless of
    which conversion happens to finish first.
    """

    def _make_dxf(self, path, x_offset):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=GAS_LAYER)
        doc.modelspace().add_lwpolyline(
            [(x_offset, 0), (x_offset + 10, 0)], dxfattribs={"layer": GAS_LAYER}
        )
        doc.saveas(str(path))

    def test_final_order_is_canonical_even_when_the_slowest_conversion_sorts_first(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        main_dwg = bundle_dir / "main.dwg"
        main_dwg.write_bytes(b"fake")
        xrefs = bundle_dir / "Xrefs"
        xrefs.mkdir()
        # Alphabetically, a_sibling sorts before b_sibling -- _other_bundle_members
        # returns them in that order, so that is the canonical order this
        # test expects in the final result.
        sibling_a = xrefs / "a_sibling.dwg"
        sibling_b = xrefs / "b_sibling.dwg"
        sibling_a.write_bytes(b"fake")
        sibling_b.write_bytes(b"fake")

        # Outside bundle_dir on purpose: _other_bundle_members recursively
        # globs the whole source folder for .dxf/.dwg, so a "converted
        # output" fixture placed *inside* it would be swept in as a spurious
        # extra bundle member.
        converted_dir = tmp_path / "converted"
        converted_dir.mkdir()
        converted_main = converted_dir / "converted_main.dxf"
        converted_a = converted_dir / "converted_a.dxf"
        converted_b = converted_dir / "converted_b.dxf"
        self._make_dxf(converted_main, x_offset=0)
        self._make_dxf(converted_a, x_offset=100)
        self._make_dxf(converted_b, x_offset=200)

        conversion_map = {
            main_dwg: converted_main,
            sibling_a: converted_a,
            sibling_b: converted_b,
        }

        def fake_convert(path, workdir):
            # Rigged the opposite way from canonical/alphabetical order:
            # the file that sorts FIRST (a_sibling) finishes converting
            # LAST, and the one that sorts SECOND (b_sibling) finishes
            # FIRST. If the implementation ever collected results in
            # completion order instead of canonical order, this setup
            # would produce [main, b, a] instead of the required [main, a, b].
            import time

            if path == sibling_a:
                time.sleep(0.15)
            return conversion_map[path]

        utilities, zones, warnings = resolve_and_read_bundle(
            bundle_dir, tmp_path / "work", layer_map=MOSGEOTREST_LAYER_MAP, convert=fake_convert
        )

        assert warnings == []
        x_offsets = [u.geometry.coords[0][0] for u in utilities]
        assert x_offsets == [0, 100, 200], "result order must be canonical, not completion order"

    def test_a_failed_sibling_conversion_is_a_warning_not_a_crash(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        main_dwg = bundle_dir / "main.dwg"
        main_dwg.write_bytes(b"fake")
        xrefs = bundle_dir / "Xrefs"
        xrefs.mkdir()
        bad_sibling = xrefs / "bad_sibling.dwg"
        bad_sibling.write_bytes(b"fake")

        converted_dir = tmp_path / "converted"
        converted_dir.mkdir()
        converted_main = converted_dir / "converted_main.dxf"
        self._make_dxf(converted_main, x_offset=0)

        def fake_convert(path, workdir):
            if path == bad_sibling:
                raise RuntimeError("simulated conversion failure")
            return converted_main

        utilities, zones, warnings = resolve_and_read_bundle(
            bundle_dir, tmp_path / "work", layer_map=MOSGEOTREST_LAYER_MAP, convert=fake_convert
        )

        assert len(warnings) == 1
        assert "bad_sibling.dwg" in warnings[0]
        # The main file's own utility still made it through.
        assert len(utilities) == 1

    def test_the_main_files_own_conversion_failure_propagates(self, tmp_path):
        """Asymmetric on purpose, matching resolve_bundle_inputs: losing the
        main drawing is a hard failure, not a warning to quietly work around."""
        main_dwg = tmp_path / "main.dwg"
        main_dwg.write_bytes(b"fake")

        def fake_convert(path, workdir):
            raise RuntimeError("main drawing is corrupt")

        with pytest.raises(RuntimeError, match="main drawing is corrupt"):
            resolve_and_read_bundle(tmp_path, tmp_path / "work", layer_map=MOSGEOTREST_LAYER_MAP, convert=fake_convert)

    def test_already_dxf_bundle_members_need_no_conversion_call_at_all(self, tmp_path):
        """A bundle mixing an already-.dxf main with .dxf siblings must not
        route anything through `convert` -- confirms the "hand it to the read
        pool immediately" path for non-.dwg files is really taken."""
        main_dxf = tmp_path / "main.dxf"
        self._make_dxf(main_dxf, x_offset=0)
        xrefs = tmp_path / "Xrefs"
        xrefs.mkdir()
        sibling_dxf = xrefs / "sibling.dxf"
        self._make_dxf(sibling_dxf, x_offset=50)

        def fake_convert(path, workdir):
            raise AssertionError(f"convert() must not be called for a .dxf file, got {path}")

        utilities, zones, warnings = resolve_and_read_bundle(
            main_dxf, tmp_path / "work", layer_map=MOSGEOTREST_LAYER_MAP, convert=fake_convert
        )

        assert warnings == []
        assert sorted(u.geometry.coords[0][0] for u in utilities) == [0, 50]

    def test_on_progress_fires_at_zero_before_conversion_and_reaches_the_total_at_the_end(self, tmp_path):
        """Live complaint this covers: a real upload's progress bar sat on
        the caller's PRE-bundle stage label ("Распаковка архива") for the
        entire main-file conversion + first-file-read stretch, which on a
        slow-converting real file reads as hung, not busy.
        `on_progress` must fire at (0, total) before the main file's own
        (potentially slow) conversion even starts, not only once files start
        finishing, so the caller has the *correct* stage name and total on
        screen immediately instead of a stale, unrelated one."""
        bundle_dir = tmp_path / "bundle"
        bundle_dir.mkdir()
        main_dwg = bundle_dir / "main.dwg"
        main_dwg.write_bytes(b"fake")
        xrefs = bundle_dir / "Xrefs"
        xrefs.mkdir()
        sibling = xrefs / "sibling.dwg"
        sibling.write_bytes(b"fake")

        converted_dir = tmp_path / "converted"
        converted_dir.mkdir()
        converted_main = converted_dir / "converted_main.dxf"
        converted_sibling = converted_dir / "converted_sibling.dxf"
        self._make_dxf(converted_main, x_offset=0)
        self._make_dxf(converted_sibling, x_offset=100)
        conversion_map = {main_dwg: converted_main, sibling: converted_sibling}

        calls: list[tuple[int, int]] = []

        resolve_and_read_bundle(
            bundle_dir,
            tmp_path / "work",
            layer_map=MOSGEOTREST_LAYER_MAP,
            convert=lambda path, workdir: conversion_map[path],
            on_progress=lambda done, total: calls.append((done, total)),
        )

        assert calls[0] == (0, 2), "must report 0/total before conversion starts, not only after files finish"
        assert calls[-1] == (2, 2)
        assert all(total == 2 for _done, total in calls), "total must not change mid-run"
        dones = [done for done, _total in calls]
        assert dones == sorted(dones), "done count must never go backwards"
