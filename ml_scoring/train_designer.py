"""Обучение модели «где бы посадил проектировщик» (ml_scoring/designer_model.py).

    python -m ml_scoring.train_designer --pilot "<…>/Пилотный проект 20 улиц"

Данные — улицы с эталоном из eval/pilot_streets.yaml. На каждой:
* кандидаты — сетка с шагом GRID_STEP_M внутри площади, где дереву можно
  стоять по нашим нормам (та же buildable_area, что строит генератор, для
  самой узкокронной породы — самое широкое допустимое множество);
* метка 1 — в радиусе LABEL_RADIUS_M есть дерево проектировщика;
* признаки — DesignerFeatures (расстояния до объектов чертежа).

Улицы весят одинаково (иначе «Олимпийская деревня» с тысячей деревьев
задавила бы остальные). Качество — leave-one-street-out: модель для улицы
обучается без неё, и ROC-AUC / average precision считаются на ней. Те же
LOSO-модели сохраняются в артефакт, чтобы scripts/evaluate.py мог оценить
план улицы моделью, которая эту улицу не видела.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from pathlib import Path

import joblib
import numpy as np
from scipy.spatial import cKDTree
import shapely
from shapely import STRtree, contains_xy, prepare
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score

from geo_engine.buffers import build_exclusion_zone, buildable_area
from geo_engine.io.dwg_convert import DWG_CACHE_ENV
from geo_engine.norms import load_norms
from geo_engine.planner import norms_for_species
from geo_engine.species import load_catalogue
from ml_scoring.designer_model import DEFAULT_ARTIFACT, FEATURE_NAMES, LABEL_RADIUS_M, DesignerFeatures
from scripts.evaluate import DEFAULT_CACHE, StreetData, load_manifest, load_street, reference_trees

GRID_STEP_M = 2.0
DESIGNER_LOSO_FILE = "designer_loso.joblib"
# Подобран перебором на leave-one-street-out оценке планов (scripts/evaluate.py
# --scoring designer --gate …): максимум среднего F1 по улицам с эталоном.
DEFAULT_GATE: float | None = 0.015


def tree_buildable(street_id: str, data: StreetData, cache: Path):
    """Площадь, где по нашим нормам может стоять узкокронное дерево, с кэшем."""
    cached = cache / "buildable" / f"{street_id}.pkl"
    if cached.is_file():
        with cached.open("rb") as f:
            return pickle.load(f)
    norms = load_norms()
    catalogue = load_catalogue()
    narrow = next(s for s in catalogue.for_type("tree") if s.crown == "narrow")
    type_norms = norms_for_species(norms, "tree", narrow, catalogue)
    exclusion = build_exclusion_zone(
        data.utilities, data.zones, "tree", type_norms, narrow, catalogue.crown_reference_diameter_m, clip_to=data.territory
    )
    buildable = buildable_area(
        data.territory, exclusion, data.zones, territory_margin_m=type_norms.territory_margin_for("tree")
    )
    cached.parent.mkdir(parents=True, exist_ok=True)
    with cached.open("wb") as f:
        pickle.dump(buildable, f, protocol=pickle.HIGHEST_PROTOCOL)
    return buildable


def street_samples(street: dict, pilot: Path, cache: Path) -> dict | None:
    ref = reference_trees(street, pilot, cache)
    if ref is None or len(ref) == 0:
        return None
    data = load_street(street, pilot, cache)
    inside = contains_xy(data.territory.buffer(1.0), ref[:, 0], ref[:, 1])
    ref = ref[inside]
    if len(ref) == 0:
        return None
    buildable = tree_buildable(street["id"], data, cache)
    prepare(buildable)
    minx, miny, maxx, maxy = buildable.bounds
    gx, gy = np.meshgrid(np.arange(minx, maxx, GRID_STEP_M), np.arange(miny, maxy, GRID_STEP_M))
    grid = np.column_stack([gx.ravel(), gy.ravel()])
    grid = grid[contains_xy(buildable, grid[:, 0], grid[:, 1])]
    dist, _ = cKDTree(ref).query(grid)
    labels = (dist <= LABEL_RADIUS_M).astype(np.int8)
    featurizer = DesignerFeatures(data.utilities, data.zones, data.territory)
    features = featurizer.matrix(grid)
    ref_in_buildable = contains_xy(buildable.buffer(LABEL_RADIUS_M), ref[:, 0], ref[:, 1])

    # Широкая выборка для обучения: вся территория, кроме того, что точно не
    # место для дерева (здания, проезжая часть, тротуары, существующая
    # зелень). Деревья проектировщика в основном стоят вне НАШЕЙ допустимой
    # площади (он сажает ближе таблиц), и узкая выборка видит только малую их
    # часть; где он сажает вообще — сигнал полнее. Проверка качества — всё
    # равно на узкой выборке отложенной улицы, где реально выбирает генератор.
    tminx, tminy, tmaxx, tmaxy = data.territory.bounds
    wx, wy = np.meshgrid(np.arange(tminx, tmaxx, GRID_STEP_M), np.arange(tminy, tmaxy, GRID_STEP_M))
    wide = np.column_stack([wx.ravel(), wy.ravel()])
    prepare(data.territory)
    wide = wide[contains_xy(data.territory, wide[:, 0], wide[:, 1])]
    blocked = [
        z.geometry
        for z in data.zones
        if z.zone_type in ("building", "road", "sidewalk", "existing_greenery") and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]
    if blocked and len(wide):
        inside, _ = STRtree(blocked).query(shapely.points(wide), predicate="within")
        keep = np.ones(len(wide), dtype=bool)
        keep[inside] = False
        wide = wide[keep]
    wdist, _ = cKDTree(ref).query(wide) if len(wide) else (np.zeros(0), None)
    return {
        "id": street["id"],
        "X": features,
        "y": labels,
        "X_wide": featurizer.matrix(wide) if len(wide) else np.zeros((0, len(FEATURE_NAMES))),
        "y_wide": (wdist <= LABEL_RADIUS_M).astype(np.int8),
        "xy_wide": wide,
        "xy": grid,
        "ref_in_territory": int(len(ref)),
        "ref_reachable": int(ref_in_buildable.sum()),
        "buildable_m2": float(buildable.area),
    }


def make_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=40, l2_regularization=1.0, random_state=0
    )


def _weights(street_ids: np.ndarray) -> np.ndarray:
    """Каждая улица в сумме весит одинаково."""
    weights = np.ones(len(street_ids))
    for sid in np.unique(street_ids):
        mask = street_ids == sid
        weights[mask] = 1.0 / mask.sum()
    return weights * len(street_ids) / weights.sum()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--pilot", type=Path, default=os.environ.get("GREENPROJECT_PILOT_DATA"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--space",
        choices=["territory", "buildable"],
        default="territory",
        help="где брать обучающие точки: вся территория без зданий и проезжей части или только наша допустимая площадь",
    )
    parser.add_argument(
        "--gate",
        type=float,
        default=DEFAULT_GATE,
        help="порог вероятности: место ниже порога генератор не займёт (подбирается scripts/evaluate.py --gate)",
    )
    args = parser.parse_args(argv)
    if not args.pilot:
        parser.error("укажите --pilot или GREENPROJECT_PILOT_DATA")
    os.environ.setdefault(DWG_CACHE_ENV, str(args.cache / "dwg"))

    t0 = time.time()
    samples = []
    for street in load_manifest():
        if not street.get("reference"):
            continue
        s = street_samples(street, args.pilot, args.cache)
        if s is None:
            print(f"{street['id']}: нет эталона внутри участка — пропуск")
            continue
        samples.append(s)
        print(
            f"{s['id']}: точек {len(s['y'])}, положительных {int(s['y'].sum())}, "
            f"деревьев проектировщика {s['ref_in_territory']} (в нашей допустимой площади {s['ref_reachable']}), "
            f"допустимо {s['buildable_m2']:.0f} м²",
            flush=True,
        )

    key_x, key_y = ("X_wide", "y_wide") if args.space == "territory" else ("X", "y")
    X = np.vstack([s[key_x] for s in samples])
    y = np.concatenate([s[key_y] for s in samples])
    groups = np.concatenate([[s["id"]] * len(s[key_y]) for s in samples])
    weights = _weights(groups)
    print(f"обучение на «{args.space}»: точек {len(y)}, положительных {int(y.sum())}", flush=True)

    report: dict = {"features": list(FEATURE_NAMES), "streets": {}}
    loso_models = {}
    for s in samples:
        train = groups != s["id"]
        model = make_model().fit(X[train], y[train], sample_weight=weights[train])
        loso_models[s["id"]] = model
        if s["y"].min() == s["y"].max():
            continue
        p = model.predict_proba(s["X"])[:, 1]
        # Базовая линия без обучения: «ближе к проезжей части — лучше».
        baseline = -s["X"][:, FEATURE_NAMES.index("d_road")]
        report["streets"][s["id"]] = {
            "auc": round(float(roc_auc_score(s["y"], p)), 3),
            "ap": round(float(average_precision_score(s["y"], p)), 3),
            "positive_rate": round(float(s["y"].mean()), 4),
            "auc_baseline_road": round(float(roc_auc_score(s["y"], baseline)), 3),
        }
        print(f"LOSO {s['id']}: {report['streets'][s['id']]}", flush=True)

    aucs = [v["auc"] for v in report["streets"].values()]
    aps = [v["ap"] for v in report["streets"].values()]
    lift = [v["ap"] / v["positive_rate"] for v in report["streets"].values()]
    report["mean_auc"] = round(float(np.mean(aucs)), 3)
    report["mean_ap"] = round(float(np.mean(aps)), 3)
    report["mean_ap_lift"] = round(float(np.mean(lift)), 2)
    report["mean_auc_baseline_road"] = round(float(np.mean([v["auc_baseline_road"] for v in report["streets"].values()])), 3)

    final = make_model().fit(X, y, sample_weight=weights)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    report["space"] = args.space
    report["gate"] = args.gate
    # В поставляемый артефакт — только итоговая модель и порог (~1 МБ);
    # модели «без одной улицы» нужны лишь scripts/evaluate.py и живут в кэше.
    # Средняя частота метки с теми же весами, что при обучении: вероятность
    # модели откалибрована к ней, и обоснование посадки говорит «в N раз
    # чаще среднего», а не голую вероятность редкого события.
    base_rate = float(np.sum(weights * y) / np.sum(weights))
    report["base_rate"] = round(base_rate, 4)
    joblib.dump(
        {"all": final, "gate": args.gate, "base_rate": base_rate, "features": list(FEATURE_NAMES), "report": report},
        args.out,
    )
    joblib.dump(loso_models, args.cache / DESIGNER_LOSO_FILE)
    (args.cache / "results").mkdir(parents=True, exist_ok=True)
    (args.cache / "results" / "designer_training.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in report.items() if k != "streets"}, ensure_ascii=False))
    print(f"модель: {args.out} ({time.time() - t0:.0f} с)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
