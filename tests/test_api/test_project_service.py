"""project_service.parse_territory_file's DWG/ZIP handling.

Focused on the parsing/dispatch logic (pure functions, no DB) -- the upload
route and create_project_from_file's DB writes aren't covered here, matching
this repo's existing convention of testing parsing separately from persistence
(see geo_engine.io.dxf_reader's own tests).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import ezdxf
import pytest

from backend.app.services.project_service import UnsupportedFileTypeError, parse_territory_file
from geo_engine.io.dwg_convert import available_backend


def _drawing(path: Path) -> None:
    """A minimal but realistic drawing: a work boundary and a gas main."""
    doc = ezdxf.new(setup=True)
    for layer in ("!Граница работ", "Газопровод"):
        doc.layers.add(name=layer)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": "!Граница работ"})
    msp.add_lwpolyline([(0, 40), (100, 40)], dxfattribs={"layer": "Газопровод"})
    doc.saveas(str(path))


def _boundary_only(path: Path) -> None:
    doc = ezdxf.new(setup=True)
    doc.layers.add(name="!Граница работ")
    doc.modelspace().add_lwpolyline(
        [(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": "!Граница работ"}
    )
    doc.saveas(str(path))


def _zip_dir(source_dir: Path, zip_path: Path, wrap_in_subfolder: bool = False) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        for file in source_dir.rglob("*"):
            if file.is_file():
                arcname = file.relative_to(source_dir)
                if wrap_in_subfolder:
                    arcname = Path(source_dir.name) / arcname
                archive.write(file, arcname)


class TestDwgUpload:
    @pytest.mark.skipif(
        available_backend() is not None,
        reason="проверяется ветка «конвертера нет», а в этом окружении он есть",
    )
    def test_a_lone_dwg_fails_cleanly_without_a_converter(self, tmp_path):
        """No LibreDWG/ODA on PATH -- must not surface as a raw traceback, it's
        an entirely expected, user-facing situation.

        Пропускается там, где конвертер установлен. Изначально тест исходил из
        того, что его нет «ни на машинах разработки, ни в CI» — на момент
        написания так и было, но `infra/Dockerfile.backend` с тех пор собирает
        LibreDWG прямо в образ, и в нём тест падал не из-за дефекта, а из-за
        того, что проверяемая ветка кода просто недостижима. Это стык двух
        веток, а не регрессия: одна добавила конвертер в образ, вторая —
        тест на его отсутствие.
        """
        dwg = tmp_path / "site.dwg"
        dwg.write_bytes(b"not a real dwg, just needs the right suffix to reach the converter check")

        with pytest.raises(UnsupportedFileTypeError, match="конвертер не найден"):
            parse_territory_file(dwg, tmp_path / "work")

    @pytest.mark.skipif(
        available_backend() is None,
        reason="нужен установленный конвертер DWG",
    )
    def test_a_corrupt_dwg_fails_cleanly_with_a_converter(self, tmp_path):
        """Обратная ветка: конвертер есть, но файл не DWG.

        Без неё в окружении с конвертером (то есть в собранном образе backend)
        обработка битого DWG не проверялась вообще — а именно там она и
        работает на живых загрузках.
        """
        dwg = tmp_path / "site.dwg"
        dwg.write_bytes(b"not a real dwg, just needs the right suffix to reach the converter check")

        with pytest.raises(UnsupportedFileTypeError):
            parse_territory_file(dwg, tmp_path / "work")


class TestZipUpload:
    def test_bundle_zip_merges_main_drawing_and_xrefs(self, tmp_path):
        site = tmp_path / "site"
        (site / "Xrefs").mkdir(parents=True)
        _drawing(site / "plan.dxf")
        _boundary_only(site / "Xrefs" / "boundary.dxf")

        zip_path = tmp_path / "site.zip"
        _zip_dir(site, zip_path)

        utilities, zones, crs = parse_territory_file(zip_path, tmp_path / "work")

        assert crs is None
        assert any(u.object_type == "gas_pipe" for u in utilities)
        territories = [z for z in zones if z.zone_type == "territory"]
        assert len(territories) >= 1

    def test_zip_wrapping_everything_in_one_subfolder_still_resolves(self, tmp_path):
        """A folder zipped by dragging it into Finder/Explorer typically nests
        its contents one level down inside a directory named after the
        folder, rather than at the archive root."""
        site = tmp_path / "site"
        site.mkdir()
        _drawing(site / "plan.dxf")

        zip_path = tmp_path / "site.zip"
        _zip_dir(site, zip_path, wrap_in_subfolder=True)

        utilities, zones, _ = parse_territory_file(zip_path, tmp_path / "work")
        assert any(u.object_type == "gas_pipe" for u in utilities)
        assert any(z.zone_type == "territory" for z in zones)

    def test_corrupt_zip_fails_cleanly(self, tmp_path):
        bad_zip = tmp_path / "broken.zip"
        bad_zip.write_bytes(b"this is not a zip file")

        with pytest.raises(UnsupportedFileTypeError, match="повреждён"):
            parse_territory_file(bad_zip, tmp_path / "work")


class TestUnsupportedSuffix:
    def test_unknown_extension_lists_dwg_and_zip_as_supported(self, tmp_path):
        other = tmp_path / "site.txt"
        other.write_text("nope")

        with pytest.raises(UnsupportedFileTypeError, match=r"\.dwg.*\.zip|\.zip.*\.dwg"):
            parse_territory_file(other)
