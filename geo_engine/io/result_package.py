"""Результат, который открывается в AutoCAD так же, как исходный проект.

Замечание экспертов «на некоторых данных криво формируется итоговый DXF»
сошлось на двух причинах:

* экспорт из веба писал ПУСТОЙ чертёж — кружки посадок без подосновы и без
  обоснований в XDATA (`write_dxf` без `base_dxf`), хотя ТЗ требует результат
  отдельным слоем поверх исходного чертежа;
* CLI клал результат поверх «самого содержательного» файла бандла, а у
  проектов, где главный чертёж — только набор внешних ссылок (10-14 объектов
  у «15. Понтрягина», «17. Грузинская», «20. Макеева»), это оказывался
  одиночный лист топосъёмки — без остального проекта.

Главный чертёж хранит ссылки ОТНОСИТЕЛЬНЫМИ путями от своей папки
(`.\\xref\\xref_граница.dwg`, `Xrefs\\xref196297.dwg` — проверено на пилоте).
Поэтому правильный результат — главный чертёж с нашими слоями, а рядом —
исходные файлы ссылок по тем же относительным путям: AutoCAD подгрузит все
листы сам, в исходном качестве, без потерь на конвертации DWG -> DXF.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

DRAWING_SUFFIXES = (".dwg", ".dxf")


def reference_targets(main: Path, members: list[Path], dest_dir: Path) -> dict[Path, Path]:
    """Куда положить каждый файл бандла, кроме главного, чтобы ссылки из
    главного чертежа, лежащего в `dest_dir`, нашлись.

    Путь сохраняется относительно папки главного чертежа. Ссылка, ведущая
    выше неё (`..\\00_Ссылки\\...` у «3-я Парковая»), кладётся прямо в
    `dest_dir`: выходить за папку результата нельзя, а AutoCAD, не найдя
    файл по сохранённому пути, ищет его по имени в папке самого чертежа.
    """
    main = Path(main).resolve()
    base = main.parent
    targets: dict[Path, Path] = {}
    for member in members:
        member = Path(member).resolve()
        if member == main or member.suffix.lower() not in DRAWING_SUFFIXES:
            continue
        relative = Path(os.path.relpath(member, base))
        if relative.parts and relative.parts[0] == "..":
            relative = Path(relative.name)
        targets[member] = Path(dest_dir) / relative
    return targets


def copy_references(main: Path, members: list[Path], dest_dir: Path) -> list[Path]:
    """Скопировать файлы ссылок рядом с результатом (см. `reference_targets`)."""
    copied: list[Path] = []
    for source, target in reference_targets(main, members, dest_dir).items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
        copied.append(target)
    return copied


def xref_attachment_count(dxf_path: Path) -> int:
    """Сколько внешних ссылок прикреплено к чертежу. Главный чертёж проекта
    обычно маленький (ссылки и рамка), так что открыть его целиком дёшево."""
    import ezdxf
    import ezdxf.recover

    # Сначала обычное чтение: на тяжёлом главном чертеже («3. 3-я Парковая»,
    # 175 МБ) recover тратил ~150 с только на то, чтобы сосчитать ссылки.
    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception:  # noqa: BLE001 — не прочиталось обычным способом
        try:
            doc, _ = ezdxf.recover.readfile(str(dxf_path))
        except Exception:  # noqa: BLE001 — нечитаемый чертёж ссылок не несёт
            return 0
    return sum(1 for block in doc.blocks if block.block_record.is_xref)
