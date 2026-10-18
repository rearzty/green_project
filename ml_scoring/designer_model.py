"""«Где бы посадил проектировщик» — модель ранжирования мест под дерево,
обученная на реальных проектных решениях пилотного датасета.

Чем отличается от MLScorer (ml_scoring/ml_scorer.py): тот обучен на
синтетике с метками «балл эвристики плюс шум» и по сути повторяет эвристику.
Здесь метка настоящая: в радиусе LABEL_RADIUS_M от точки проектировщик
поставил дерево (деревья из посадочных чертежей «Проектного решения», см.
eval/pilot_streets.yaml).

Что модель делает и чего не делает. Допустимость места по нормам решает
geo_engine (детерминированно, с цитатой акта) — модель видит только уже
допустимые точки и только ранжирует их: «чёрный ящик» в допустимости ТЗ
запрещает. Признаки — расстояния до того, что есть в чертеже (проезжая
часть, тротуар, здания, граница участка, сети, существующая зелень), то есть
то же, на что смотрит проектировщик, когда выбирает место для аллеи.

Обучение и честная проверка — `python -m ml_scoring.train_designer`: модель
для каждой улицы обучается без этой улицы (leave-one-street-out), и
scripts/evaluate.py оценивает план улицы моделью, которая её не видела.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from geo_engine.model import PlantingCandidate, Utility, Zone
from geo_engine.norms import PlantingNorms
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.scoring_strategy import ScoringStrategy

DEFAULT_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "designer_model.joblib"

LABEL_RADIUS_M = 2.0
# Ниже этой доли участка, пропускаемой моделью, отсечение выключается — см.
# DesignerScorer.__init__.
MIN_GATE_PASS_SHARE = 0.01
MAX_DISTANCE_M = 60.0

# Группы объектов чертежа -> признак «расстояние до ближайшего из них».
_ZONE_GROUPS: dict[str, tuple[str, ...]] = {
    "road": ("road", "tram_track"),
    "sidewalk": ("sidewalk", "sidewalk_kerb"),
    "building": ("building", "school_kindergarten"),
    "greenery": ("existing_greenery",),
    "lawn": ("existing_lawn",),
    "pole": ("lighting_pole",),
}
_UTILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "pipe": ("heat_network", "water_pipe", "sewer", "gas_pipe"),
    "cable": ("cable_line", "power_line_corridor"),
}

# Сколько объектов группы в радиусе — плотность окружения: сквер с сетью
# дорожек, двор у домов и улица с одной линией борта выглядят по-разному.
_COUNT_FEATURES: dict[str, tuple[str, float]] = {
    "n_greenery_10": ("d_greenery", 10.0),
    "n_sidewalk_15": ("d_sidewalk", 15.0),
    "n_building_30": ("d_building", 30.0),
}

FEATURE_NAMES: tuple[str, ...] = (
    *(f"d_{name}" for name in _ZONE_GROUPS),
    *(f"d_{name}" for name in _UTILITY_GROUPS),
    "d_edge",
    *_COUNT_FEATURES,
    # Полоса между проезжей частью и тротуаром — типичное место аллеи.
    "strip_width",
    "kerb_minus_walk",
)

FEATURE_LABELS_RU = {
    "d_road": "проезжая часть",
    "d_sidewalk": "тротуар",
    "d_building": "здание",
    "d_greenery": "существующая зелень",
    "d_lawn": "существующий газон",
    "d_pole": "опора освещения",
    "d_pipe": "трубопроводы",
    "d_cable": "кабели",
    "d_edge": "граница участка",
}
_DISTANCE_FEATURES = tuple(name for name in FEATURE_NAMES if name.startswith("d_"))


def _edge_segments(geom: BaseGeometry) -> list:
    """Граница участка, порезанная на 2-точечные отрезки: расстояние до
    сложной границы с тысячами вершин иначе считалось бы по всем вершинам
    на каждую точку (тот же приём, что geo_engine.candidates.ExclusionIndex)."""
    segments = []
    for line in shapely.get_parts(shapely.boundary(geom)):
        coords = shapely.get_coordinates(line)
        if len(coords) >= 2:
            segments.extend(shapely.linestrings(np.stack([coords[:-1], coords[1:]], axis=1)))
    return segments


@dataclass
class _Index:
    tree: STRtree | None


def _nearest_distance(index: _Index, points: np.ndarray) -> np.ndarray:
    out = np.full(len(points), MAX_DISTANCE_M)
    if index.tree is None or len(points) == 0:
        return out
    (src, _), dist = index.tree.query_nearest(points, max_distance=MAX_DISTANCE_M, return_distance=True, all_matches=False)
    out[src] = np.minimum(dist, MAX_DISTANCE_M)
    return out


def _count_within(index: _Index, points: np.ndarray, radius: float) -> np.ndarray:
    out = np.zeros(len(points))
    if index.tree is None or len(points) == 0:
        return out
    src, _ = index.tree.query(points, predicate="dwithin", distance=radius)
    np.add.at(out, src, 1.0)
    return out


class DesignerFeatures:
    """Пространственные индексы одной улицы; строятся один раз, потом
    признаки для любого числа точек — векторно."""

    def __init__(self, utilities: list[Utility], zones: list[Zone], territory: BaseGeometry) -> None:
        def build(geoms: list) -> _Index:
            geoms = [g for g in geoms if g is not None and not g.is_empty]
            return _Index(STRtree(geoms) if geoms else None)

        self._indexes: dict[str, _Index] = {}
        for name, types in _ZONE_GROUPS.items():
            self._indexes[f"d_{name}"] = build([z.geometry for z in zones if z.zone_type in types])
        for name, types in _UTILITY_GROUPS.items():
            self._indexes[f"d_{name}"] = build([u.geometry for u in utilities if u.object_type in types])
        self._indexes["d_edge"] = build(_edge_segments(territory))

    def matrix(self, xy: np.ndarray) -> np.ndarray:
        points = shapely.points(xy)
        columns = {name: _nearest_distance(self._indexes[name], points) for name in _DISTANCE_FEATURES}
        for name, (index_name, radius) in _COUNT_FEATURES.items():
            columns[name] = _count_within(self._indexes[index_name], points, radius)
        columns["strip_width"] = np.minimum(columns["d_road"] + columns["d_sidewalk"], MAX_DISTANCE_M)
        columns["kerb_minus_walk"] = columns["d_road"] - columns["d_sidewalk"]
        return np.column_stack([columns[name] for name in FEATURE_NAMES])


def load_artifact(path: Path = DEFAULT_ARTIFACT) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"Модель по проектным решениям не обучена ({path}). Запустите: python -m ml_scoring.train_designer"
        )
    return _load_artifact_cached(str(path), path.stat().st_mtime_ns)


@lru_cache(maxsize=4)
def _load_artifact_cached(path: str, _mtime_ns: int) -> dict:
    """Артефакт читается один раз на процесс (и заново, если файл переобучили):
    веб создаёт скорер на каждую генерацию."""
    return joblib.load(path)


class DesignerScorer(ScoringStrategy):
    """Деревья — по модели проектировщика, кустарник и газон — эвристикой
    (эталона по ним нет, см. eval/pilot_streets.yaml)."""

    def __init__(
        self,
        norms: PlantingNorms,
        utilities: list[Utility],
        zones: list[Zone],
        territory: BaseGeometry,
        artifact_path: Path = DEFAULT_ARTIFACT,
        exclude_street: str | None = None,
        gate: float | None = None,
        loso_path: Path | None = None,
    ) -> None:
        artifact = load_artifact(artifact_path)
        # Порог «похоже на выбор проектировщика»: место с вероятностью ниже
        # отклоняется (отрицательная оценка, см. placement.greedy_select).
        # Подобран по leave-one-street-out оценке планов (scripts/evaluate.py).
        self._gate = artifact.get("gate") if gate is None else gate
        self._base_rate = artifact.get("base_rate")
        self._model = artifact["all"]
        if exclude_street:
            # Для честной оценки на пилотной улице — модель, обученная без неё
            # (ml_scoring/train_designer.py кладёт такие модели в кэш оценки,
            # а не в поставляемый артефакт).
            loso = artifact.get("loso")
            if loso is None and loso_path is not None and loso_path.is_file():
                loso = joblib.load(loso_path)
            if loso is None or exclude_street not in loso:
                raise ValueError(f"нет модели, обученной без улицы {exclude_street!r} — запустите ml_scoring.train_designer")
            self._model = loso[exclude_street]
        self._features = DesignerFeatures(utilities, zones, territory)
        # Территория, совсем непохожая на улицы пилота (синтетика, другой тип
        # объекта), — модель пропускает почти ничего, и с отсечением план
        # остался бы без деревьев. Тогда отсечение для этой территории
        # выключается, а модель только ранжирует. На улицах пилота модель
        # пропускает от 3,5 до 55 % участка, на синтетике — 0 %.
        self.gate_pass_share = self._pass_share(territory) if self._gate is not None else None
        if self.gate_pass_share is not None and self.gate_pass_share < MIN_GATE_PASS_SHARE:
            self._gate = None
        self._fallback = HeuristicScorer(norms, existing_greenery=[z.geometry for z in zones if z.zone_type == "existing_greenery"])

    def _pass_share(self, territory: BaseGeometry) -> float:
        """Доля участка, которую модель пропускает при текущем пороге (по
        сетке не больше ~20 тыс. точек)."""
        if territory is None or territory.is_empty:
            return 0.0
        minx, miny, maxx, maxy = territory.bounds
        step = max(2.0, float(np.sqrt(territory.area / 20_000)))
        gx, gy = np.meshgrid(np.arange(minx, maxx, step), np.arange(miny, maxy, step))
        xy = np.column_stack([gx.ravel(), gy.ravel()])
        xy = xy[shapely.contains_xy(territory, xy[:, 0], xy[:, 1])]
        if len(xy) == 0:
            return 0.0
        proba = self._model.predict_proba(self._features.matrix(xy))[:, 1]
        return float(np.mean(proba >= self._gate))

    def score(self, candidate: PlantingCandidate) -> tuple[float, str]:
        return self.score_batch([candidate])[0]

    def score_batch(self, candidates: list[PlantingCandidate]) -> list[tuple[float, str]]:
        tree_idx = [i for i, c in enumerate(candidates) if c.planting_type == "tree" and c.geometry.geom_type == "Point"]
        results: list[tuple[float, str] | None] = [None] * len(candidates)
        if tree_idx:
            xy = np.array([(candidates[i].geometry.x, candidates[i].geometry.y) for i in tree_idx])
            X = self._features.matrix(xy)
            proba = self._model.predict_proba(X)[:, 1]
            for row, i in enumerate(tree_idx):
                p = float(proba[row])
                if self._gate is not None and p < self._gate:
                    results[i] = (-1.0, "Отклонено моделью по проектным решениям: место непохоже на выбор проектировщика.")
                else:
                    results[i] = (p, _rationale(p, X[row], self._base_rate))
        rest = [i for i, r in enumerate(results) if r is None]
        if rest:
            for i, scored in zip(rest, self._fallback.score_batch([candidates[i] for i in rest])):
                results[i] = scored
        return results  # type: ignore[return-value]


def _rationale(probability: float, row: np.ndarray, base_rate: float | None = None) -> str:
    near = sorted(
        (
            (value, FEATURE_LABELS_RU[name])
            for name, value in zip(FEATURE_NAMES, row)
            if name in FEATURE_LABELS_RU and value < MAX_DISTANCE_M
        ),
        key=lambda pair: pair[0],
    )[:2]
    context = f" Рядом: {', '.join(f'{label} {value:.1f} м' for value, label in near)}." if near else ""
    if base_rate:
        lift = probability / base_rate
        verdict = (
            f"в похожих местах проектировщики сажают деревья в {lift:.1f} раза чаще среднего"
            if lift >= 1
            else f"в похожих местах проектировщики сажают деревья реже среднего ({lift:.1f} от среднего)"
        )
    else:
        verdict = f"место похоже на выбор проектировщика с оценкой {probability:.2f}"
    return f"Модель по проектным решениям пилота: {verdict}.{context}"
