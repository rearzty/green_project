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


def test_missing_references_lists_only_files_absent_by_path_and_by_name(tmp_path):
    from geo_engine.io.result_package import missing_references

    (tmp_path / "xref").mkdir()
    (tmp_path / "xref" / "a.dwg").write_bytes(b"")
    (tmp_path / "b.dwg").write_bytes(b"")

    missing = missing_references([r".\xref\a.dwg", r"..\other\b.dwg", "Штриховка.dwg", ""], tmp_path)

    assert missing == ["Штриховка.dwg"]


def test_a_reference_saved_under_another_folder_is_placed_where_the_drawing_looks(tmp_path):
    """«6. Камчатская»: чертёж ищет ./игди/x.dwg, в поставке файл лежит в
    ./Внешние ссылки/игди/x.dwg."""
    from geo_engine.io.result_package import missing_references, place_by_saved_paths

    source = tmp_path / "src" / "Внешние ссылки" / "игди"
    source.mkdir(parents=True)
    (source / "x.dwg").write_bytes(b"x")
    dest = tmp_path / "out"
    dest.mkdir()

    placed = place_by_saved_paths([r".\игди\x.dwg", r"C:\abs\y.dwg"], [source / "x.dwg"], dest)

    assert placed == 1 and (dest / "игди" / "x.dwg").exists()
    assert missing_references([r".\игди\x.dwg"], dest) == []
