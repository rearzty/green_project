"""Свести чертёж вместе с его Xrefs/ в один самодостаточный DXF.

Зачем: реальный проектный чертёж не самодостаточен — граница участка и
инженерные сети лежат в отдельных файлах рядом, а веб-загрузка принимает один
файл. CLI (`scripts/plan_dxf.py`) читает каталог целиком и в этом не нуждается;
эта утилита нужна, чтобы получить файл, который можно отдать в форму загрузки
или приложить к демонстрации.

    python -m scripts.flatten_bundle --input <каталог|чертёж> --output site.dxf

На выходе — DXF с распознанными объектами на слоях, которые понимает
`COMBINED_LAYER_MAP` при обратном чтении: сети по типам, здания, дороги,
существующее озеленение, граница участка. Это НЕ копия исходного чертежа со
всем оформлением: рамки, штампы, выноски и подписи не переносятся — только
геометрия, на которой работает алгоритм.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

import ezdxf
from shapely.geometry.base import BaseGeometry

from geo_engine.io.dxf_reader import COMBINED_LAYER_MAP, read_dxf_bundle
from geo_engine.io.dxf_writer import RESULT_LAYER_PREFIX
from geo_engine.territory import TERRITORY_ZONE_TYPE
from scripts.plan_dxf import resolve_inputs

# object_type/zone_type -> имя слоя, которое MOSGEOTREST_LAYER_MAP прочитает
# обратно в тот же тип. Обратная операция к карте слоёв, поэтому имена взяты
# из неё дословно, а не придуманы заново.
LAYER_FOR_TYPE = {
    "gas_pipe": "Газопровод",
    "heat_network": "Теплосеть",
    "water_pipe": "Водопровод",
    "sewer": "Канализация самотёчная",
    "cable_line": "Кабель электрический",
    "power_line_corridor": "ЛЭП",
    "building": "Здания",
    "road": "Бортовой камень",
    "existing_greenery": "Леса и газоны",
    TERRITORY_ZONE_TYPE: "!Граница работ",
}


def _write_geometry(msp, geometry: BaseGeometry, layer: str) -> int:
    """Одна геометрия -> сущности DXF. Возвращает, сколько записано."""
    kind = geometry.geom_type
    if kind == "Point":
        msp.add_point((geometry.x, geometry.y), dxfattribs={"layer": layer})
        return 1
    if kind == "LineString":
        points = [(x, y) for x, y, *_ in (c + (0,) if len(c) == 2 else c for c in geometry.coords)]
        if len(points) < 2:
            return 0
        msp.add_lwpolyline(points, dxfattribs={"layer": layer})
        return 1
    if kind == "Polygon":
        written = 0
        exterior = [(x, y) for x, y, *_ in (c + (0,) if len(c) == 2 else c for c in geometry.exterior.coords)]
        if len(exterior) >= 3:
            msp.add_lwpolyline(exterior, close=True, dxfattribs={"layer": layer})
            written += 1
        for ring in geometry.interiors:
            hole = [(x, y) for x, y, *_ in (c + (0,) if len(c) == 2 else c for c in ring.coords)]
            if len(hole) >= 3:
                msp.add_lwpolyline(hole, close=True, dxfattribs={"layer": layer})
                written += 1
        return written
    parts = getattr(geometry, "geoms", None)
    if parts is None:
        return 0
    return sum(_write_geometry(msp, part, layer) for part in parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.flatten_bundle",
        description="Свести чертёж и его Xrefs/ в один самодостаточный DXF для загрузки через веб-форму.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Каталог с чертежом и Xrefs/ либо один чертёж")
    parser.add_argument("--output", required=True, type=Path, help="Куда записать сведённый DXF")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="greenplan-flat-") as tmp:
        print(f"Чтение: {args.input}")
        _, bundle = resolve_inputs(args.input, Path(tmp))
        print(f"  файлов в бандле: {len(bundle)}")
        utilities, zones = read_dxf_bundle(
            bundle, layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True
        )

    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()
    written = Counter()
    skipped = Counter()

    for utility in utilities:
        layer = LAYER_FOR_TYPE.get(utility.object_type)
        if layer is None:
            skipped[utility.object_type] += 1
            continue
        if layer not in doc.layers:
            doc.layers.add(name=layer)
        written[utility.object_type] += _write_geometry(msp, utility.geometry, layer)

    for zone in zones:
        layer = LAYER_FOR_TYPE.get(zone.zone_type)
        if layer is None:
            skipped[zone.zone_type] += 1
            continue
        if layer not in doc.layers:
            doc.layers.add(name=layer)
        written[zone.zone_type] += _write_geometry(msp, zone.geometry, layer)

    if not written.get(TERRITORY_ZONE_TYPE):
        print(
            "\nВНИМАНИЕ: границы участка в бандле не нашлось — этот файл не даст построить план.",
            file=sys.stderr,
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(args.output))

    print(f"\nЗаписано в {args.output} ({args.output.stat().st_size / 1e6:.1f} МБ)")
    for object_type, count in written.most_common():
        print(f"  {count:7}  {object_type} -> слой «{LAYER_FOR_TYPE[object_type]}»")
    if skipped:
        # Не «unknown» ради шума: это типы, для которых у нас нет ни норматива,
        # ни зоны — их отсутствие в файле ничего не меняет для алгоритма.
        print(f"  пропущено (нет сопоставления): {dict(skipped.most_common(5))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
