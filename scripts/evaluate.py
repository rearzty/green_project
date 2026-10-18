"""Оценка генератора на пилотном датасете против решений проектировщиков.

    python -m scripts.evaluate --pilot "<…>/Пилотный проект 20 улиц" [--streets olimp,kharkovskaya]
        [--scoring heuristic|ml|designer] [--jobs 4]

Что делает, по улицам из eval/pilot_streets.yaml:
1. достаёт деревья проектировщика из его посадочного чертежа (эталон);
2. читает вход улицы так же, как scripts/plan_dxf.py (тот же бандл, те же
   флаги очистки), и кэширует прочитанное — повторные прогоны не тратят
   минуты на конвертацию и разбор DWG;
3. строит план деревьев тем же plan_items, что CLI и веб;
4. сравнивает с эталоном.

Метрики (по деревьям, внутри границы участка):
* ref_legal_share — доля деревьев проектировщика, которые проходят НАШУ
  проверку отступов (для самой узкокронной породы, без учёта интервала между
  соседями). Проектировщики сажают ближе таблиц, с защитными мерами; это число
  говорит, какую часть эталона норма вообще позволяет повторить;
* recall@R — доля допустимых деревьев проектировщика, рядом с которыми (ближе
  R м) у нас тоже стоит дерево: «нашли ли мы его места»;
* precision@R — доля наших деревьев, рядом с которыми есть дерево
  проектировщика: «сажаем ли мы там, где сажал бы он»;
* count_ratio — сколько у нас деревьев на одно его (по допустимым).

Генерация детерминирована (один plan_key), так что разница между режимами
скоринга — эффект самого скоринга, а не случайной раскладки.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml
from scipy.spatial import cKDTree
from shapely import contains_xy
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from geo_engine.compliance import NEIGHBOUR_OBJECT_TYPE, explain_items
from geo_engine.io.dwg_convert import DWG_CACHE_ENV, convert_dwg_to_dxf
from geo_engine.io.dxf_reader import COMBINED_LAYER_MAP, read_document, read_dxf_bundle
from geo_engine.model import PlantingItem, Utility, Zone
from geo_engine.norms import load_norms
from geo_engine.planner import ROW_RATIONALE_PREFIX, plan_items
from geo_engine.species import load_catalogue
from geo_engine.territory import MissingTerritoryError, territory_polygon

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "eval" / "pilot_streets.yaml"
DEFAULT_CACHE = REPO_ROOT / "data" / "eval_cache"
PLAN_KEY = "eval"
MATCH_RADIUS_M = 3.0


@dataclass
class StreetData:
    utilities: list[Utility]
    zones: list[Zone]
    territory: BaseGeometry
    unreadable: list[str]


def load_manifest(path: Path = MANIFEST) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["streets"]


def reference_trees(street: dict, pilot: Path, cache: Path) -> np.ndarray | None:
    """Деревья проектировщика, Nx2 в координатах чертежа (они же координаты
    входа — все листы улицы в одной системе, см. CLAUDE.md)."""
    spec = street.get("reference")
    if not spec:
        return None
    cached = cache / "reference" / f"{street['id']}.json"
    if cached.is_file():
        return np.asarray(json.loads(cached.read_text())["xy"], dtype=float).reshape(-1, 2)
    layer_re = re.compile(spec["layer"])
    block_re = re.compile(spec["block"]) if spec.get("block") else None
    with tempfile.TemporaryDirectory() as tmp:
        dxf = convert_dwg_to_dxf(pilot / spec["file"], tmp)
        doc = read_document(dxf)
        points = []
        for insert in doc.modelspace().query("INSERT"):
            if not layer_re.search(insert.dxf.layer):
                continue
            if block_re and not block_re.search(insert.dxf.name):
                continue
            p = insert.ocs().to_wcs(insert.dxf.insert)
            points.append((round(p.x, 2), round(p.y, 2)))
    xy = np.unique(np.asarray(points, dtype=float).reshape(-1, 2), axis=0)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps({"source": spec["file"], "xy": xy.tolist()}))
    return xy


def load_street(street: dict, pilot: Path, cache: Path) -> StreetData:
    """Вход улицы ровно так, как его читает scripts/plan_dxf.py, с кэшем."""
    cached = cache / "inputs" / f"{street['id']}.pkl"
    if cached.is_file():
        with cached.open("rb") as f:
            return StreetData(**pickle.load(f))
    from scripts.plan_dxf import resolve_inputs

    unreadable: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        _, bundle = resolve_inputs(pilot / street["input"], Path(tmp))
        utilities, zones = read_dxf_bundle(
            bundle,
            layer_map=COMBINED_LAYER_MAP,
            stitch_dashes=True,
            drop_origin=True,
            reconstruct_footprints=True,
            on_error=lambda path, error: unreadable.append(path.name),
        )
    territory = territory_polygon(zones, utilities)
    data = StreetData(utilities, zones, territory, unreadable)
    cached.parent.mkdir(parents=True, exist_ok=True)
    with cached.open("wb") as f:
        # Словарь, а не сам StreetData: при запуске через `python -m` класс
        # живёт в __main__, и такой pickle не открыть из другого модуля.
        pickle.dump(data.__dict__, f, protocol=pickle.HIGHEST_PROTOCOL)
    return data


def make_scorer(scoring: str, norms, data: StreetData, street_id: str | None = None, gate: float | None = None):
    existing_greenery = [z.geometry for z in data.zones if z.zone_type == "existing_greenery"]
    if scoring == "heuristic":
        from ml_scoring.heuristic_scorer import HeuristicScorer

        return HeuristicScorer(norms, existing_greenery=existing_greenery)
    if scoring == "ml":
        from ml_scoring.ml_scorer import MLScorer

        return MLScorer(norms, existing_greenery=existing_greenery)
    if scoring == "designer":
        from ml_scoring.designer_model import DesignerScorer

        # Модель, обученная БЕЗ этой улицы (leave-one-street-out) — иначе
        # оценка на ней была бы подглядыванием в ответ.
        from ml_scoring.train_designer import DESIGNER_LOSO_FILE

        return DesignerScorer(
            norms, data.utilities, data.zones, data.territory,
            exclude_street=street_id, gate=gate, loso_path=DEFAULT_CACHE / DESIGNER_LOSO_FILE,
        )
    raise ValueError(f"unknown scoring mode {scoring!r}")


def legal_mask(xy: np.ndarray, data: StreetData, norms) -> tuple[np.ndarray, dict[str, int]]:
    """Какие точки проходят нашу проверку отступов для самой узкокронной
    породы, и какое ограничение чаще всего мешает остальным (по самому
    сильному нарушению в каждой точке). Интервал до соседних деревьев не
    учитывается: он про пары посадок внутри одного плана, а не про место."""
    if len(xy) == 0:
        return np.zeros(0, dtype=bool), {}
    catalogue = load_catalogue()
    narrow = next((s for s in catalogue.for_type("tree") if s.crown == "narrow"), catalogue.for_type("tree")[0])
    items = [PlantingItem(Point(x, y), "tree", narrow.name, 0.0, "") for x, y in xy]
    records = explain_items(items, data.utilities, data.zones, norms, catalogue)
    mask = np.zeros(len(records), dtype=bool)
    blockers: dict[str, int] = {}
    for i, record in enumerate(records):
        failed = [c for c in record.checks if c.object_type != NEIGHBOUR_OBJECT_TYPE and not c.satisfied]
        mask[i] = not failed
        if failed:
            worst = min(failed, key=lambda c: c.actual_m - c.required_m)
            blockers[worst.object_type] = blockers.get(worst.object_type, 0) + 1
    return mask, dict(sorted(blockers.items(), key=lambda kv: -kv[1]))


def compare(ours: np.ndarray, ref: np.ndarray, radius: float) -> dict:
    if len(ref) == 0 or len(ours) == 0:
        return {"recall": 0.0, "precision": 0.0, "f1": 0.0}
    d_ref, _ = cKDTree(ours).query(ref)
    d_ours, _ = cKDTree(ref).query(ours)
    recall = float(np.mean(d_ref <= radius))
    precision = float(np.mean(d_ours <= radius))
    f1 = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
    return {"recall": recall, "precision": precision, "f1": f1, "median_nn_m": float(np.median(d_ref))}


def evaluate_street(street: dict, pilot: Path, cache: Path, scoring: str, radius: float, gate: float | None = None) -> dict:
    t0 = time.time()
    result: dict = {"id": street["id"], "name": street["name"], "scoring": scoring}
    try:
        data = load_street(street, pilot, cache)
    except (MissingTerritoryError, SystemExit, Exception) as error:  # noqa: BLE001
        result["error"] = f"{type(error).__name__}: {error}"[:300]
        return result
    norms = load_norms()
    result["territory_m2"] = round(data.territory.area)
    result["unreadable_files"] = len(data.unreadable)

    scorer = make_scorer(scoring, norms, data, street["id"], gate)
    items = plan_items(PLAN_KEY, data.utilities, data.zones, data.territory, ["tree"], scorer.as_score_fn(), norms)
    trees = [i for i in items if i.planting_type == "tree"]
    ours = np.array([(i.geometry.x, i.geometry.y) for i in trees]).reshape(-1, 2)
    records = explain_items(trees, data.utilities, data.zones, norms) if trees else []
    plan_path = cache / "plans" / scoring / f"{street['id']}.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(np.round(ours, 2).tolist()))
    result["trees"] = len(trees)
    result["trees_per_ha"] = round(len(trees) / (data.territory.area / 10_000), 1)
    result["compliance"] = round(sum(r.compliant for r in records) / len(records), 4) if records else 1.0
    result["row_share"] = round(sum(ROW_RATIONALE_PREFIX in i.rationale for i in trees) / len(trees), 3) if trees else 0.0

    ref = reference_trees(street, pilot, cache)
    if ref is not None:
        inside = contains_xy(data.territory.buffer(1.0), ref[:, 0], ref[:, 1]) if len(ref) else np.zeros(0, bool)
        ref_in = ref[inside]
        legal, blockers = legal_mask(ref_in, data, norms)
        result["ref_blockers"] = blockers
        result["ref_trees"] = int(len(ref))
        result["ref_in_territory"] = int(len(ref_in))
        result["ref_legal_share"] = round(float(legal.mean()), 3) if len(legal) else None
        ref_legal = ref_in[legal]
        result["count_ratio"] = round(len(ours) / len(ref_legal), 2) if len(ref_legal) else None
        result.update({k: round(v, 3) for k, v in compare(ours, ref_legal, radius).items()})
        # Против ВСЕХ его деревьев на участке, не только допустимых по нашим
        # нормам: знаменатель не зависит от того, как мы читаем чертёж, —
        # этим сравнивать версии пайплайна между собой.
        overall = compare(ours, ref_in, radius)
        result["recall_all_ref"] = round(overall["recall"], 3)
        result["precision_all_ref"] = round(overall["precision"], 3)
        result["f1_all_ref"] = round(overall["f1"], 3)
    result["seconds"] = round(time.time() - t0, 1)
    return result


def _format_table(rows: list[dict]) -> str:
    cols = [
        ("name", "улица"),
        ("territory_m2", "участок, м²"),
        ("trees", "наших"),
        ("ref_in_territory", "у проектир."),
        ("ref_legal_share", "доп. по нормам"),
        ("count_ratio", "наших/его"),
        ("recall", "recall@R"),
        ("precision", "precision@R"),
        ("f1", "F1"),
        ("compliance", "нормы"),
        ("seconds", "с"),
    ]
    lines = ["| " + " | ".join(h for _, h in cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        if "error" in row:
            lines.append(f"| {row['name']} | ошибка: {row['error'][:80]} |")
            continue
        lines.append("| " + " | ".join("" if row.get(k) is None else str(row.get(k)) for k, _ in cols) + " |")
    return "\n".join(lines)


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if r.get("ref_legal_share") is not None and "error" not in r]
    if not scored:
        return {}
    return {
        "streets_with_reference": len(scored),
        "mean_recall": round(float(np.mean([r["recall"] for r in scored])), 3),
        "mean_precision": round(float(np.mean([r["precision"] for r in scored])), 3),
        "mean_f1": round(float(np.mean([r["f1"] for r in scored])), 3),
        "mean_ref_legal_share": round(float(np.mean([r["ref_legal_share"] for r in scored])), 3),
        "mean_recall_all_ref": round(float(np.mean([r["recall_all_ref"] for r in scored])), 3),
        "mean_precision_all_ref": round(float(np.mean([r["precision_all_ref"] for r in scored])), 3),
        "mean_f1_all_ref": round(float(np.mean([r["f1_all_ref"] for r in scored])), 3),
        "trees_total": int(sum(r["trees"] for r in scored)),
        "min_compliance": min(r["compliance"] for r in rows if "compliance" in r),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pilot", type=Path, default=os.environ.get("GREENPROJECT_PILOT_DATA"))
    parser.add_argument("--streets", help="id через запятую (по умолчанию — все из манифеста)")
    parser.add_argument("--only-reference", action="store_true", help="только улицы с эталоном")
    parser.add_argument("--scoring", default="heuristic", choices=["heuristic", "ml", "designer"])
    parser.add_argument("--radius", type=float, default=MATCH_RADIUS_M)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--jobs", type=int, default=1, help="улиц параллельно (после того, как вход уже в кэше)")
    parser.add_argument("--out", type=Path, help="куда записать JSON с результатами")
    parser.add_argument("--gate", type=float, help="порог модели проектировщика (по умолчанию — из артефакта)")
    args = parser.parse_args(argv)
    if not args.pilot:
        parser.error("укажите --pilot или GREENPROJECT_PILOT_DATA")
    os.environ.setdefault(DWG_CACHE_ENV, str(args.cache / "dwg"))

    streets = load_manifest()
    if args.streets:
        wanted = set(args.streets.split(","))
        streets = [s for s in streets if s["id"] in wanted]
    if args.only_reference:
        streets = [s for s in streets if s.get("reference")]

    rows: list[dict] = []
    if args.jobs > 1:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(evaluate_street, s, args.pilot, args.cache, args.scoring, args.radius, args.gate) for s in streets]
            for future in futures:
                rows.append(future.result())
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    else:
        for street in streets:
            rows.append(evaluate_street(street, args.pilot, args.cache, args.scoring, args.radius, args.gate))
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    print()
    print(_format_table(rows))
    summary = summarize(rows)
    print()
    print(json.dumps(summary, ensure_ascii=False))
    out = args.out or args.cache / "results" / f"{args.scoring}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "streets": rows, "radius_m": args.radius}, ensure_ascii=False, indent=1))
    print(f"\nрезультаты: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
