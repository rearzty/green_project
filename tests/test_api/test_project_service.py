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
    def test_a_lone_dwg_fails_cleanly_without_a_converter(self, tmp_path):
        """No LibreDWG/ODA on PATH in this environment (dev machines and CI
        alike don't have it installed by default) -- must not surface as a
        raw traceback, it's an entirely expected, user-facing situation."""
        dwg = tmp_path / "site.dwg"
        dwg.write_bytes(b"not a real dwg, just needs the right suffix to reach the converter check")

        with pytest.raises(UnsupportedFileTypeError, match="конвертер не найден"):
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


class TestZipFilenameEncoding:
    """Кириллица в именах внутри архива.

    Спецификация ZIP знает две кодировки имён: CP437 и UTF-8, вторую — только
    если выставлен бит 11. Ни Finder, ни Info-ZIP `zip`, ни проводник Windows
    его не ставят, а имена пишут в UTF-8 — архив лжёт о себе, и `zipfile`
    честно декодирует UTF-8-байты как CP437.

    Поймано живьём на реальной загрузке: `archive.extractall()` упал
    `OSError: [Errno 36] File name too long` на обычном архиве папки улицы.
    Настоящее имя — 229 байт, после ложного разбора — 566 при лимите в 255.
    То есть ZIP-загрузка не работала ровно на том входе, ради которого
    делалась.
    """

    def _zip_without_utf8_flag(self, zip_path: Path, names: list[str]) -> None:
        """Архив, в котором имена — UTF-8, а бит 11 не выставлен.

        Ровно то, что делает Finder/`zip`/проводник. Собирается вручную:
        `zipfile` при записи сам выставит флаг, как только увидит не-ASCII,
        поэтому флаг снимается уже после записи, в заголовках.
        """
        with zipfile.ZipFile(zip_path, "w") as archive:
            for name in names:
                archive.writestr(name, b"x")
        with zipfile.ZipFile(zip_path, "a") as archive:
            for info in archive.infolist():
                info.flag_bits &= ~0x800

        raw = zip_path.read_bytes()
        # Бит 11 живёт в обоих местах: в локальном заголовке (сигнатура
        # PK\x03\x04, смещение 6) и в записи центрального каталога
        # (PK\x01\x02, смещение 8). Правится побайтово, потому что zipfile
        # не даёт записать заголовок с флагом, который сам считает неверным.
        patched = bytearray(raw)
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = 0
            while (found := patched.find(signature, start)) != -1:
                flags = int.from_bytes(patched[found + offset:found + offset + 2], "little")
                patched[found + offset:found + offset + 2] = (flags & ~0x800).to_bytes(2, "little")
                start = found + 4
        zip_path.write_bytes(bytes(patched))

    def test_a_cyrillic_name_survives_extraction(self, tmp_path):
        from backend.app.services.project_service import _extract_archive, _zip_member_name

        name = "Генеральный план редформат/чертёж.dxf"
        zip_path = tmp_path / "bundle.zip"
        self._zip_without_utf8_flag(zip_path, [name])

        with zipfile.ZipFile(zip_path) as archive:
            info = archive.infolist()[0]
            assert info.filename != name, "фикстура обязана воспроизводить ложную кодировку"
            assert _zip_member_name(info) == name

            out = tmp_path / "out"
            out.mkdir()
            _extract_archive(archive, out)

        assert (out / "Генеральный план редформат" / "чертёж.dxf").exists()

    def test_a_long_cyrillic_name_no_longer_overflows_the_filesystem_limit(self, tmp_path):
        """Тот самый отказ: имя короче лимита, а после ложного разбора — нет."""
        from backend.app.services.project_service import _extract_archive

        stem = "Генеральный план. М1-500 (Совмещен с разбивочным планом и планом покрытий)"
        assert len(stem.encode("utf-8")) < 255
        assert len(stem.encode("utf-8").decode("cp437").encode("utf-8")) > 255

        zip_path = tmp_path / "bundle.zip"
        self._zip_without_utf8_flag(zip_path, [f"{stem}.dxf"])
        out = tmp_path / "out"
        out.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            _extract_archive(archive, out)

        assert (out / f"{stem}.dxf").exists()

    def test_an_honest_utf8_archive_is_untouched(self, tmp_path):
        """Архив с правильно выставленным флагом трогать нельзя."""
        from backend.app.services.project_service import _extract_archive

        name = "папка/чертёж.dxf"
        zip_path = tmp_path / "bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(name, b"x")  # zipfile сам выставит бит 11 на не-ASCII

        out = tmp_path / "out"
        out.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            assert archive.infolist()[0].flag_bits & 0x800, "фикстура должна быть честной"
            _extract_archive(archive, out)

        assert (out / "папка" / "чертёж.dxf").exists()

    def test_a_member_escaping_the_extract_dir_is_skipped(self, tmp_path):
        """zip-slip: ручная распаковка обязана сохранить защиту, которая была
        в extractall."""
        from backend.app.services.project_service import _extract_archive

        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("../escaped.txt", b"x")
            archive.writestr("ok.txt", b"x")

        out = tmp_path / "out"
        out.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            _extract_archive(archive, out)

        assert (out / "ok.txt").exists()
        assert not (tmp_path / "escaped.txt").exists()
