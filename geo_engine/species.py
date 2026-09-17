"""Каталог пород: атрибуты, от которых зависят отступы и интервалы.

Раньше это был плоский список правдоподобных названий, присваиваемый уже
расставленным точкам постфактум — чистая косметика. После сверки с
нормативами так нельзя: и отступ, и интервал зависят от породы.

  * МГСН 1.02-02, п. 4.2.8 — отступ от теплотрассы задан поимённым списком
    пород, а не одним числом на «дерево»;
  * 743-ПП, табл. 3.6.1, прим. 3 — дерево с широкой кроной, затеняющее жилые
    помещения, отодвигается от здания на 10 м вместо 5;
  * 743-ПП, табл. 3.6.1, прим. 1 (и то же в СП 42.13330.2016, табл. 9.1,
    прим. 1) — нормативы даны для кроны не более 5 м и «должны быть
    соответственно увеличены» для более крупной;
  * МГСН 1.02-02, п. 4.2.9.2 — интервал между стволами по классу кроны;
  * 369-ПП, приложение 1 — перечень инвазивных видов; вид оттуда предлагать
    к посадке нельзя.

Сравнение с перечнем инвазивных идёт по ПОЛНОМУ названию, не по роду. Это не
придирка: «Клен ясенелистный» инвазивен, а «Клён остролистный» — основная
уличная порода Москвы и назван в тех же актах как рекомендуемый; «Дуб
красный» инвазивен, «Дуб черешчатый» нет. Сопоставление по первому слову
запретило бы половину нормального ассортимента.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_SPECIES_PATH = Path(__file__).parent / "config" / "species.yaml"

CrownClass = str  # "narrow" | "medium" | "wide"

_PLANTING_SECTIONS = ("tree", "shrub")


def _normalize(name: str) -> str:
    """Для сравнения названий: регистр и «ё»/«е» не должны решать.

    В нормативных актах и в чертежах одно и то же пишется по-разному —
    в 369-ПП «Дерен белый», у нас было «Дёрен белый», и это один вид.
    """
    return " ".join(name.lower().replace("ё", "е").split())


class InvasiveSpecies(BaseModel):
    ru: str
    la: str = ""
    group: int | None = None


class Species(BaseModel):
    """Одна порода со всем, что влияет на размещение."""

    name: str
    crown: CrownClass = "medium"
    crown_diameter_m: float | None = None
    # Персональный минимум от теплосети (МГСН 4.2.8). None — порода в акте не
    # названа, применяется общий отступ из planting_norms.yaml.
    heat_network_min_m: float | None = None
    # Широкая крона, затеняющая жилые помещения (743-ПП табл. 3.6.1 прим. 3).
    shades_windows: bool = False
    # Плотность групповой посадки, шт/м² (515-ПП, табл. 5 приложения).
    density_per_m2: float | None = None

    def setback_multiplier(self, reference_diameter_m: float) -> float:
        """Во сколько раз увеличить табличный отступ под крупную крону.

        743-ПП табл. 3.6.1 прим. 1 / СП 42.13330.2016 табл. 9.1 прим. 1:
        «нормативы относятся к деревьям с диаметром кроны не более 5 м и
        должны быть соответственно увеличены для деревьев большего диаметра».
        Что значит «соответственно», акт не раскрывает — берётся линейная
        пропорция по диаметру, самое прямое прочтение. Меньше 1.0 никогда:
        узкая крона не даёт права придвинуться ближе таблицы.
        """
        if not self.crown_diameter_m or reference_diameter_m <= 0:
            return 1.0
        return max(1.0, self.crown_diameter_m / reference_diameter_m)


class SpeciesCatalogue(BaseModel):
    invasive: list[InvasiveSpecies] = []
    crown_spacing_m: dict[CrownClass, float] = {}
    crown_reference_diameter_m: float = 5.0
    tree: list[Species] = []
    shrub: list[Species] = []

    def for_type(self, planting_type: str) -> list[Species]:
        """Породы, допустимые для этого типа посадки. `lawn` их не имеет."""
        return list(getattr(self, planting_type, []) or [])

    def names_for_type(self, planting_type: str) -> list[str]:
        return [s.name for s in self.for_type(planting_type)]

    def get(self, name: str | None) -> Species | None:
        if not name:
            return None
        target = _normalize(name)
        for section in _PLANTING_SECTIONS:
            for species in getattr(self, section, []) or []:
                if _normalize(species.name) == target:
                    return species
        return None

    def spacing_for_crown(self, crown: CrownClass) -> float | None:
        return self.crown_spacing_m.get(crown)

    def is_invasive(self, name: str | None) -> InvasiveSpecies | None:
        """Запись перечня 369-ПП, если вид в него внесён.

        Сопоставление по полному названию — см. docstring модуля о том, почему
        не по роду. Латинское название тоже принимается: в чертежах и
        ведомостях вид иногда подписан им.
        """
        if not name:
            return None
        target = _normalize(name)
        for entry in self.invasive:
            if _normalize(entry.ru) == target:
                return entry
            if entry.la and _normalize(entry.la) == target:
                return entry
        return None

    def invasive_in_assortment(self) -> list[tuple[str, InvasiveSpecies]]:
        """Предлагаемые к посадке виды, попавшие в перечень 369-ПП.

        Должно быть пусто — проверяется тестом. Метод существует, чтобы
        проверка была одна и та же в тесте и в рантайме, а не переписывалась
        дважды: перечень обновляется постановлениями, и однажды в нём окажется
        вид, который сегодня в ассортименте.
        """
        found = []
        for section in _PLANTING_SECTIONS:
            for species in getattr(self, section, []) or []:
                entry = self.is_invasive(species.name)
                if entry is not None:
                    found.append((species.name, entry))
        return found


@lru_cache(maxsize=8)
def load_catalogue(path: Path | str = DEFAULT_SPECIES_PATH) -> SpeciesCatalogue:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return SpeciesCatalogue.model_validate(raw)


@lru_cache(maxsize=8)
def load_species(path: Path | str = DEFAULT_SPECIES_PATH) -> dict[str, list[str]]:
    """Прежняя форма — только имена по типу посадки.

    Сохранена потому, что существующий код (pipeline_service) выбирает из неё
    случайное имя, и ломать этот путь одновременно с переходом на каталог
    незачем.
    """
    catalogue = load_catalogue(path)
    return {section: catalogue.names_for_type(section) for section in _PLANTING_SECTIONS}
