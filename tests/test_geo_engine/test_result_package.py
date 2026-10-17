"""Куда класть файлы ссылок рядом с результатом."""

from __future__ import annotations

from pathlib import Path

from geo_engine.io.result_package import reference_targets


def test_references_keep_their_path_relative_to_the_main_drawing(tmp_path):
    root = tmp_path / "site"
    main = root / "main.dwg"
    members = [main, root / "xref" / "a.dwg", root / "xref_ИТП" / "tp.dwg", root / "notes.txt"]
    for p in members:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    targets = reference_targets(main, members, tmp_path / "out")

    assert targets == {
        (root / "xref" / "a.dwg").resolve(): tmp_path / "out" / "xref" / "a.dwg",
        (root / "xref_ИТП" / "tp.dwg").resolve(): tmp_path / "out" / "xref_ИТП" / "tp.dwg",
    }


def test_a_reference_above_the_main_folder_lands_next_to_the_result(tmp_path):
    """«3. 3-я Парковая»: главный чертёж в 02_АПОТ/, ссылки в ../00_Ссылки/.
    Выходить за папку результата нельзя — AutoCAD найдёт файл по имени рядом."""
    root = tmp_path / "site"
    main = root / "02_АПОТ" / "main.dwg"
    ref = root / "00_Ссылки" / "Границы работ.dwg"
    for p in (main, ref):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    targets = reference_targets(main, [main, ref], tmp_path / "out")

    assert targets == {ref.resolve(): tmp_path / "out" / "Границы работ.dwg"}
