"""Юна без языковой модели: встроенный разбор частых просьб по-русски.

Жюри соберёт Docker-образ из репозитория без всякого ключа, и чат при этом
должен работать, а не отвечать 503 с именем переменной окружения. Здесь
детерминированно разбирается то, о чём реально просят на показе: объяснить
план по фактам (assistant_context.PlanFacts), поменять рецепт генерации,
выделить группу посадок, поправить выделенное, скачать результат. Тот же
разбор отвечает и когда модель настроена, но недоступна (лимит бесплатного
тарифа, сеть): роут переключается на него сам.

Это не NLU и не попытка им быть: регулярные выражения по нормализованному
тексту (регистр, ё→е, пунктуация). Сообщение режется на части по запятым и
союзам — «кусты пореже и двумя рядами» это две просьбы и одна команда
настроек. Подлежащее без сказуемого достаётся соседней части («без газона и
кустов», «сколько деревьев и кустов»). Чего не поняла — честно говорит и
перечисляет, что умеет.

Здесь же — общее для обоих режимов: правила безопасности для команд
(guard_commands), что значат интервалы посадок (tree_floor, HEDGE_PITCH_M) и
описание сделанного по-русски (describe_actions). Модель часто вызывает
инструмент молча, без текста, и ответ ей собирается тем же кодом, что и здесь.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from backend.app.schemas.assistant import (
    AssistantAction,
    AssistantCommand,
    AssistantGenerationSettings,
    AssistantSelection,
    EditSelectionCommand,
    SelectCommand,
    SettingsCommand,
    UiCommand,
)
from backend.app.services.assistant_context import PlanFacts
from geo_engine.planner import IN_ROW_PITCH_M
from geo_engine.species import load_catalogue, load_species

# Те же границы, что валидирует GenerateRequest (schemas/plan.py): выйти за
# них команда не может, даже если бы разбор ошибся.
SPACING_LIMITS = {"tree": (0.5, 15.0), "shrub": (0.3, 10.0)}

# Класс кроны: «широкая крона», «широким кронам».
_CROWN_NAMES = {"wide": ("широкая", "широким"), "medium": ("средняя", "средним"), "narrow": ("узкая", "узким")}


def _crown_intervals() -> dict[str, float]:
    """Норма интервала по классу кроны, м: МГСН 1.02-02 п. 4.2.9.2 из
    species.yaml (нижние границы диапазонов 8-10/5-6/3-4 — это минимумы)."""
    try:
        found = {c: float(m) for c, m in load_catalogue().crown_spacing_m.items() if c in _CROWN_NAMES}
    except Exception:  # справка для ответа — не повод ронять чат
        found = {}
    return found or {"wide": 8.0, "medium": 5.0, "narrow": 3.0}


CROWN_INTERVALS = _crown_intervals()
# Интервал деревьев пользователя — нижний предел ПОВЕРХ нормы класса кроны, а
# не замена ей (geo_engine.planner.norms_for_species): каждое дерево стоит не
# ближе большего из двух. Гуще нормы деревья не поставить вовсе, а интервал не
# больше нормы самой узкой кроны не раздвигает ни одного дерева — это то же,
# что «по норме» (None), см. tree_floor. Раньше Юна считала «текущим»
# tree_default из planting_norms.yaml (5 м) и на «деревья погуще» отвечала
# «5 → 3,5 м» — замерено на трёх синтетических участках: число деревьев не
# менялось (27/31/28 → 27/30/28), ответ был неправдой.
TREE_NORM_MIN = min(CROWN_INTERVALS.values())
# Большая часть кустарника — живая изгородь с этим шагом (planner.IN_ROW_PITCH_M,
# а не shrub_default 3 м из planting_norms.yaml — тот для россыпи). Интервал
# пользователя заменяет шаг изгороди; куртины идут по плотности породы
# (515-ПП) и от него не зависят.
HEDGE_PITCH_M = IN_ROW_PITCH_M.get("shrub", 0.5)
# Откуда считать «пореже/погуще», когда своего интервала в рецепте нет. У
# деревьев одного текущего числа нет (по классу кроны), «пореже» начинается
# от нормы средней кроны; «погуще» без своего интервала невозможно.
FALLBACK_SPACING = {"tree": CROWN_INTERVALS.get("medium", 5.0), "shrub": HEDGE_PITCH_M}
SPARSER_FACTOR = 1.4
DENSER_FACTOR = 0.7
_SLIGHTLY_SPARSER = 1.2
_SLIGHTLY_DENSER = 0.85
# Доля участка, начиная с которой «мало посадок» — уже не про нормы, а про интервал.
_ROOMY_SHARE = 0.2

POINT_TYPES = ("tree", "shrub")
ALL_TYPES = ("tree", "shrub", "lawn")

_NOM = {"tree": "деревья", "shrub": "кусты", "lawn": "газон"}
_GEN = {"tree": "деревьев", "shrub": "кустов", "lawn": "газона"}
_INS = {"tree": "деревьями", "shrub": "кустами", "lawn": "газоном"}
_SINGULAR = {"tree": "дерево", "shrub": "кустарник", "lawn": "газон"}
_TARGET_OF = {"tree": "trees", "shrub": "shrubs", "lawn": "lawn"}
_FORMS = {
    "tree": ("дерево", "дерева", "деревьев"),
    "shrub": ("куст", "куста", "кустов"),
    "lawn": ("участок газона", "участка газона", "участков газона"),
    "plot": ("участок", "участка", "участков"),
    "object": ("объект", "объекта", "объектов"),
    "violation": ("нарушение", "нарушения", "нарушений"),
    "species": ("порода", "породы", "пород"),
    "planting": ("посадка", "посадки", "посадок"),
}

NORMS_CITATION = "СП 42.13330.2016 табл. 9.1, 743-ПП табл. 3.6.1"
NO_PLAN = "Плана пока нет — сначала сгенерируйте его («сгенерируй план»), тогда отвечу по цифрам."
NORMS_REFUSAL = (
    f"Отступы от сетей, дорог и зданий — норматив ({NORMS_CITATION}, ПУЭ), их я не могу ни ослабить, "
    "ни поменять. Могу сделать посадки гуще или реже или разрешить деревья в покрытии тротуара."
)
_EXAMPLES = (
    "«почему так мало деревьев?», «кусты пореже», «деревья двумя рядами», «покажи нарушения», "
    "«удали выделенные», «скачай DXF», «покажи в 3D»"
)
HELP_TEXT = (
    "Я Юна, помощник по плану озеленения. Сейчас я в базовом режиме — без языковой модели — "
    "и понимаю короткие просьбы:\n"
    "• объяснить план: «почему так мало деревьев?», «почему эта посадка здесь?», «сколько кустов?», "
    "«покажи нарушения»;\n"
    "• поменять генерацию: «кусты пореже», «интервал деревьев 6 м», «деревья двумя рядами», "
    "«разреши деревья в тротуаре», «без газона», «режим ML», «пересчитай план»;\n"
    "• выделение на карте: «выдели все деревья», «выдели липы», «сними выделение», «удали выделенные», "
    "«сделай их кустами»;\n"
    "• файлы и вид: «скачай DXF», «отчёт CSV», «покажи в 3D»."
)
FALLBACK_TEXT = (
    "Не поняла запрос. Сейчас я в базовом режиме — без языковой модели — и понимаю короткие просьбы, "
    f"например: {_EXAMPLES}. Напишите «что ты умеешь» — расскажу всё."
)
UNDO_TEXT = (
    "Отменить правку сама не могу — нажмите Ctrl+Z (вернуть — Ctrl+Y). Прежние варианты плана — "
    "в «Версиях плана» на панели."
)
DECLINED_TEXT = "Хорошо, не трогаю."


# --- Числа и слова ------------------------------------------------------------


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    return few if 2 <= n <= 4 else many


def fmt_int(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def fmt_num(value: float, digits: int = 2) -> str:
    """4.2 -> «4,2», 15.0 -> «15»: десятичная запятая, без хвостовых нулей."""
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text.replace(".", ",")


def count_phrase(n: int, kind: str) -> str:
    return f"{fmt_int(n)} {plural(n, *_FORMS[kind])}"


def _area(m2: float) -> str:
    return f"{fmt_int(m2)} м²"


def _share(share: float) -> str:
    return fmt_num(100 * share, 1)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " и " + words[-1]


def _short_act(act: str) -> str:
    """«Постановление Правительства Москвы № 743-ПП от 10.09.2002 (ред. …)» -> «743-ПП»."""
    match = re.search(r"№\s*([\w.-]+-ПП)", act)
    if match:
        return match.group(1)
    return re.split(r"\s+от\s+|\s+\(", act, maxsplit=1)[0]


def _first_sentence(text: str, limit: int = 160) -> str:
    sentence = (text or "").strip().split(". ")[0].strip()
    if not sentence:
        return ""
    if len(sentence) > limit:
        sentence = sentence[:limit].rsplit(" ", 1)[0] + "…"
    return sentence if sentence.endswith((".", "…", ")")) else sentence + "."


def _lower_first(text: str) -> str:
    """«Рядовая посадка…» после двоеточия — со строчной; аббревиатуру («МГСН…») не трогает."""
    return text[:1].lower() + text[1:] if text[:2].istitle() else text


def fmt_m(value: float) -> str:
    """Метры так же, как их показывает карточка посадки (InspectorCard.tsx,
    `meters`: до 10 м — два знака, дальше — один): иначе Юна говорит
    «14,16 м», а в карточке рядом — «14,2 м». Intl.NumberFormat округляет
    кратчайшую десятичную запись числа половиной вверх (1.005 -> «1,01»), а не
    его точное двоичное значение, как round() — отсюда Decimal(str(...))."""
    digits = Decimal("0.01") if value < 10 else Decimal("0.1")
    return fmt_num(float(Decimal(str(value)).quantize(digits, rounding=ROUND_HALF_UP)))


# --- Интервалы ----------------------------------------------------------------


def _crown_norms() -> str:
    ordered = sorted(CROWN_INTERVALS.items(), key=lambda kv: -kv[1])
    parts = [f"{_CROWN_NAMES[c][0]}{' крона' if i == 0 else ''} — {fmt_num(m)} м" for i, (c, m) in enumerate(ordered)]
    return "МГСН 1.02-02 п. 4.2.9.2: " + ", ".join(parts)


# «МГСН 1.02-02 п. 4.2.9.2: широкая крона — 8 м, средняя — 5 м, узкая — 3 м».
CROWN_NORMS = _crown_norms()
TREE_NORM_RANGE = f"{fmt_num(TREE_NORM_MIN)}–{fmt_num(max(CROWN_INTERVALS.values()))} м"
SHRUB_MIN_NOTE = "нижняя граница 743-ПП табл. 3.6.2 для низкой живой изгороди"


def tree_floor(value: float | None) -> float | None:
    """Интервал деревьев, который хоть что-то меняет, или None — «по норме
    класса кроны»: не больше нормы самой узкой кроны он не раздвигает ни одно
    дерево. Оба режима хранят такой интервал как None — и из «погуще» от 4 м,
    и из «интервал деревьев 2 м», и из 2,45 м, присланных моделью."""
    if value is None or value <= TREE_NORM_MIN + 1e-9:
        return None
    return value


def _tree_floor_text(value: float, old: float | None = None) -> str:
    """«не меньше 7 м (широким кронам норма — 8 м)». Где норма класса больше
    предела, дерево стоит по норме, — без этой оговорки «7 м» читалось бы как
    интервал всех деревьев."""
    text = f"не меньше {fmt_num(value)} м" + (f" вместо {fmt_num(old)}" if old is not None else "")
    wider = sorted((m, c) for c, m in CROWN_INTERVALS.items() if m > value + 1e-9)
    if wider:
        text += " (" + ", ".join(
            f"{_CROWN_NAMES[c][1]}{' кронам норма' if i == 0 else ''} — {fmt_num(m)} м" for i, (m, c) in enumerate(wider)
        ) + ")"
    return text


def _tree_offers(settings: AssistantGenerationSettings | AssistantAction) -> list[str]:
    """Что действительно может добавить деревья, если оно ещё не включено.
    «Может», а не «добавит»: интервал двухрядной аллеи — не меньше 7 м (743-ПП
    табл. 3.6.2), и там, где второй ряд не помещается, она узкие кроны даже
    прореживает (замерено на синтетических участках: 27 → 26, 31 → 29)."""
    offers = []
    if settings.tree_rows != 2:
        offers.append("двухрядная аллея на достаточно широкой полосе («деревья двумя рядами»)")
    if not settings.trees_in_paving:
        offers.append("посадка в покрытии тротуара с решёткой («разреши деревья в тротуаре»)")
    return offers


def tree_denser_text(settings: AssistantGenerationSettings | AssistantAction) -> str:
    """Ответ на «деревья погуще», когда своего интервала нет (или он не больше
    нормы узкой кроны): менять нечего, и это надо сказать прямо."""
    text = f"Гуще нормы деревья не поставить: каждое уже стоит с минимальным интервалом для своего класса кроны ({CROWN_NORMS})."
    offers = _tree_offers(settings)
    if offers:
        return text + f" Добавить деревья может {' или '.join(offers)}."
    return text + " Двухрядная аллея и деревья в покрытии тротуара уже включены."


def spacing_moves(
    planting_type: str, old: float | None, new: float | None, direction: int, default_spacing: dict[str, float]
) -> bool:
    """Сдвигает ли `new` интервал туда, куда просили (direction: -1 — гуще,
    +1 — реже), с учётом того, что значит None у каждого типа."""
    if planting_type == "tree":
        old_floor, new_floor = tree_floor(old), tree_floor(new)
        if direction < 0:
            return old_floor is not None and (new_floor is None or new_floor < old_floor)
        return new_floor is not None and (old_floor is None or new_floor > old_floor)
    base = default_spacing.get(planting_type, FALLBACK_SPACING[planting_type])
    old_value, new_value = (old if old is not None else base), (new if new is not None else base)
    return new_value < old_value if direction < 0 else new_value > old_value


# --- Породы ------------------------------------------------------------------

_ENDINGS = (
    "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ов", "ев", "ей", "ий", "ый", "ой", "ая", "яя",
    "ое", "ее", "ые", "ие", "ам", "ям", "ах", "ях", "ом", "ем", "ую", "юю", "а", "я", "ы", "и", "у", "ю",
    "е", "о", "ь", "й",
)


def _norm_words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я]+", (text or "").lower().replace("ё", "е"))


def _stem(word: str) -> str:
    """Грубая основа слова: «липы»/«липу»/«лип» -> «лип», «кленов» -> «клен».
    Названий в плане — десяток-другой, настоящий морфологический разбор
    здесь был бы зависимостью ради одной функции."""
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


def _stems_match(a: str, b: str) -> bool:
    return min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a))


def match_species(query: str, catalogue: list[tuple[str, int]]) -> list[str]:
    """Названия пород плана, похожие на `query`, лучшие первыми.

    Род («Липа») обязан совпасть; видовой эпитет только уточняет, какую из
    спирей имели в виду. Сравнение по одному роду не годится для фильтра:
    в плане бывают «Спирея серая» и «Спирея японская» сразу."""
    query_stems = [_stem(w) for w in _norm_words(query)]
    if not query_stems:
        return []
    wanted = " ".join(_norm_words(query))
    exact = [name for name, _ in catalogue if " ".join(_norm_words(name)) == wanted]
    if exact:
        return exact
    scored = []
    for rank, (name, _count) in enumerate(catalogue):
        name_stems = [_stem(w) for w in _norm_words(name)]
        if not name_stems or not any(_stems_match(q, name_stems[0]) for q in query_stems):
            continue
        score = sum(1 for q in query_stems if any(_stems_match(q, s) for s in name_stems))
        scored.append((-score, rank, name))
    return [name for *_, name in sorted(scored)]


def resolve_species(species: str, target: str, facts: PlanFacts | None) -> tuple[str, str | None] | None:
    """Породу из команды — к точному названию из плана: фронт фильтрует по
    нему. Возвращает (target, порода) — target может смениться, если
    «сирень» попросили среди деревьев, — или None, если в плане такой нет."""
    species = (species or "").strip()
    if not species or target not in ("trees", "shrubs"):
        return target, None
    if facts is None or not facts.types:
        return target, species  # сверить не с чем — пусть фильтрует фронт
    own = "tree" if target == "trees" else "shrub"
    for planting_type in (own, *(t for t in POINT_TYPES if t != own)):
        names = match_species(species, facts.species(planting_type))
        if names:
            return _TARGET_OF[planting_type], names[0]
    return None


def _mentioned_species(text: str, facts: PlanFacts | None) -> tuple[str, list[str]] | None:
    """Какая порода плана названа в свободном тексте: (тип, названия)."""
    if facts is None or not facts.types:
        return None
    tokens = [_stem(w) for w in _norm_words(text) if len(w) >= 3]
    for planting_type in POINT_TYPES:
        catalogue = facts.species(planting_type)
        genus_hits = []
        for name, _count in catalogue:
            stems = [_stem(w) for w in _norm_words(name)]
            if stems and any(_stems_match(t, stems[0]) for t in tokens):
                epithet = sum(1 for s in stems[1:] if any(_stems_match(t, s) for t in tokens))
                genus_hits.append((-epithet, name))
        if genus_hits:
            best = min(e for e, _ in genus_hits)
            exact = [name for e, name in genus_hits if e == best and e < 0]
            return planting_type, exact or [name for _, name in genus_hits]
    return None


def _catalogue_species(text: str) -> tuple[str, str] | None:
    """Порода из каталога (species.yaml), которой в плане может не быть, —
    чтобы на «выдели дубы» ответить «дубов в плане нет», а не «что выделить?»."""
    try:
        catalogue = load_species()
    except Exception:  # каталог здесь — справка для ответа, не повод ронять чат
        return None
    tokens = [_stem(w) for w in _norm_words(text) if len(w) >= 3]
    for planting_type in POINT_TYPES:
        for name in catalogue.get(planting_type, []):
            stems = [_stem(w) for w in _norm_words(name)]
            if stems and any(_stems_match(t, stems[0]) for t in tokens):
                return planting_type, name
    return None


def species_absent(name: str, planting_type: str | None, facts: PlanFacts | None) -> str:
    text = f"Породы «{name}» в плане нет"
    present = [n for t in ([planting_type] if planting_type else POINT_TYPES) for n, _ in (facts.species(t) if facts else [])]
    return text + (f" — есть: {', '.join(present[:6])}." if present else ".")


NO_VIOLATIONS = "Нарушений нет — все посадки проходят по нормам."
VIOLATIONS_PENDING = "Проверка плана на нормативы ещё не готова — подождите пару секунд и спросите снова."
NOTHING_TO_SELECT = "Плана пока нет — выделять нечего. Сначала сгенерируйте его («сгенерируй план»)."


def select_refusal(target: str, facts: PlanFacts | None, violations_count: int | None) -> str | None:
    """Почему выделять нечего, или None, если выделение имеет смысл. Общая для
    обоих режимов: модель «выделяет нарушения» и при нуле нарушений, и её
    «Выделила посадки с нарушениями» тогда неправда. О плане без посадок по
    типам (не материализован) молчит — счётчиков у него нет, «нет» было бы
    догадкой."""
    if target == "none":
        return None
    if facts is None:
        return NOTHING_TO_SELECT
    if target == "violations":
        if violations_count is None:
            return VIOLATIONS_PENDING
        return NO_VIOLATIONS if violations_count == 0 else None
    if not facts.materialized:
        return None
    if target == "all":
        return None if sum(facts.counts.values()) else "План пустой — выделять нечего."
    planting_type = {"trees": "tree", "shrubs": "shrub", "lawn": "lawn"}[target]
    return None if facts.count(planting_type) else f"{_cap(_GEN[planting_type])} в плане нет — выделять нечего."


# --- Безопасность команд (общая для модели и для разбора) --------------------


EDIT_OPERATIONS = ("delete", "to_tree", "to_shrub")


@dataclass
class Dropped:
    """Правка выделения, которую нельзя выполнить, и почему."""

    operation: str
    # nothing_selected — на карте ничего не выделено; after_select — в том же
    # ответе новое выделение, которого пользователь ещё не видел;
    # select_failed — выделить в том же ответе пытались, но не вышло (породы
    # нет, нарушений ноль); with_deselect — в том же ответе «сними
    # выделение», непонятно, что из двух имелось в виду; lawn_only — газон тип
    # не меняет; same_type — выделенные уже этого типа; asked — просьба была
    # вопросом или условием («можно удалить выделенные?»).
    reason: str


def guard_commands(
    commands: list[AssistantCommand],
    selection: AssistantSelection | None,
    *,
    select_attempted: bool = False,
    asked: Collection[str] = (),
) -> tuple[list[AssistantCommand], list[Dropped]]:
    """Правила, которые держит код, а не промпт: модель можно уговорить, код — нет.

    (a) Правка выделения без выделения отбрасывается — пусть сначала выделят.
    (b) Правка — и удаление, и смена типа — в одном ответе с выделением
        отбрасывается: править можно только то, что пользователь уже видит
        выделенным, а не то, что выделится через мгновение по догадке
        модели. Массовая правка всегда в два шага. Это касается и выделения,
        которое не удалось (`select_attempted`: породы в плане нет, нарушений
        ноль, неизвестная цель): «выдели дубы и удали их» без дубов раньше
        оставляло голое «удали» — и оно удаляло то, что выделено сейчас.
    (c) Правка из вопроса или условия (`asked` — операции, которые просили
        только так: «можно удалить выделенные?», «а если сделать их
        кустами?») не выполняется: модель и разбор выполнили бы вопрос
        буквально. Юна объясняет, что будет, и ждёт просьбы.
    «Сними выделение и удали» — ни то ни другое молча: выделение остаётся,
    чтобы следующей просьбой удалить можно было именно его.
    Ослабить нормы отступов нечем вовсе — такого инструмента нет.
    """
    count = selection.count if selection else 0
    selects = [c for c in commands if isinstance(c, SelectCommand)]
    picks = [c for c in selects if c.target != "none"]
    has_edit = any(isinstance(c, EditSelectionCommand) for c in commands)
    hold_deselect = has_edit and bool(selects) and not picks and not select_attempted and count > 0
    kept: list[AssistantCommand] = []
    dropped: list[Dropped] = []
    for command in commands:
        if isinstance(command, EditSelectionCommand):
            reason = _edit_refusal(command.operation, selection, bool(picks), select_attempted, bool(selects), asked)
            if reason:
                dropped.append(Dropped(command.operation, reason))
                continue
        elif hold_deselect and isinstance(command, SelectCommand):
            continue
        kept.append(command)
    return kept, dropped


def _edit_refusal(
    operation: str,
    selection: AssistantSelection | None,
    picks: bool,
    select_attempted: bool,
    deselect: bool,
    asked: Collection[str],
) -> str | None:
    count = selection.count if selection else 0
    if picks:
        return "after_select"
    if select_attempted:
        return "select_failed"
    if count <= 0:
        return "nothing_selected"
    if deselect:
        return "with_deselect"
    # Состав выделения известен только для текущего, не для нового — новых здесь уже нет.
    if operation != "delete" and selection is not None:
        if selection.lawns >= count:
            return "lawn_only"
        if (selection.trees if operation == "to_tree" else selection.shrubs) >= count:
            return "same_type"
    if operation in asked:
        return "asked"
    return None


# --- Описание сделанного -----------------------------------------------------


def settings_changes(
    before: AssistantGenerationSettings | AssistantAction, after: AssistantAction, default_spacing: dict[str, float]
) -> list[str]:
    parts = []
    removed = [t for t in before.planting_types if t not in after.planting_types]
    added = [t for t in after.planting_types if t not in before.planting_types]
    if removed and len(after.planting_types) == 1:
        parts.append(f"только {_NOM[after.planting_types[0]]}")
    else:
        if removed:
            parts.append("без " + _join([_GEN[t] for t in removed]))
        if added:
            parts.append("с " + _join([_INS[t] for t in added]))
    old_tree, new_tree = tree_floor(before.tree_spacing_m), tree_floor(after.tree_spacing_m)
    if old_tree != new_tree:
        # Предел, а не интервал всех деревьев: «не меньше», с нормой тех
        # классов, у которых она больше (geo_engine.planner.norms_for_species).
        if new_tree is None:
            parts.append(f"интервал деревьев — по норме класса кроны ({TREE_NORM_RANGE})")
        else:
            parts.append(f"интервал деревьев — {_tree_floor_text(new_tree, old_tree)}")
    old_shrub, new_shrub = before.shrub_spacing_m, after.shrub_spacing_m
    if old_shrub != new_shrub:
        # Интервал кустов — шаг живой изгороди; куртины от него не зависят.
        hedge = default_spacing.get("shrub", FALLBACK_SPACING["shrub"])
        if new_shrub is None:
            parts.append(f"интервал кустов — по умолчанию (живая изгородь {fmt_num(hedge)} м)")
        elif (old_shrub if old_shrub is not None else hedge) == new_shrub:
            parts.append(f"интервал кустов в живой изгороди {fmt_num(new_shrub)} м")
        else:
            parts.append(
                f"интервал кустов в живой изгороди {fmt_num(old_shrub if old_shrub is not None else hedge)} → {fmt_num(new_shrub)} м"
            )
    if after.tree_rows != before.tree_rows:
        parts.append("деревья двумя рядами" if after.tree_rows == 2 else "деревья одним рядом")
    if after.trees_in_paving != before.trees_in_paving:
        parts.append("деревья в покрытии тротуара разрешены" if after.trees_in_paving else "без деревьев в покрытии тротуара")
    if after.scoring_mode != before.scoring_mode:
        parts.append("режим ML (как проектировщик)" if after.scoring_mode == "ml" else "режим эвристики")
    return parts


def describe_settings(
    before: AssistantGenerationSettings | AssistantAction, after: AssistantAction, default_spacing: dict[str, float]
) -> str:
    parts = settings_changes(before, after, default_spacing)
    if not parts:
        return "Перестраиваю план с текущими настройками."
    return _cap(", ".join(parts)) + ", перестраиваю план."


_UI_TEXT = {
    "export_dxf": "Запускаю экспорт DXF с подосновой — файл скачается, когда будет готов.",
    "report_json": "Скачиваю обоснование каждой посадки (JSON).",
    "report_csv": "Скачиваю обоснование посадок таблицей CSV для Excel.",
    "show_3d": "Показываю план в 3D.",
    "show_2d": "Возвращаюсь на 2D-карту.",
}
_EDIT_VERB = {"delete": "Удаляю выделенное", "to_tree": "Делаю выделенное деревьями", "to_shrub": "Делаю выделенное кустами"}
_CONFIRM = {
    "delete": "скажите «удали выделенные», если хотите удалить",
    "to_tree": "скажите «сделай их деревьями», если хотите поменять тип",
    "to_shrub": "скажите «сделай их кустами», если хотите поменять тип",
}
_WILL_DO = {"delete": "удалю", "to_tree": "сделаю деревьями", "to_shrub": "сделаю кустами"}
_SAY = {"delete": "удали выделенные", "to_tree": "сделай их деревьями", "to_shrub": "сделай их кустами"}
# Выделить не вышло — правка тоже не выполнена, и это надо сказать: иначе
# непонятно, тронуто ли то, что выделено сейчас.
_NOT_DONE = {"delete": "Удалять ничего не стала.", "to_tree": "Тип не меняла.", "to_shrub": "Тип не меняла."}
_WHICH_ONE = {
    "delete": "удалить выделенное",
    "to_tree": "сделать выделенное деревьями",
    "to_shrub": "сделать выделенное кустами",
}


def _asked_text(operation: str, selection: AssistantSelection | None) -> str:
    """Ответ на «можно удалить выделенные?», «что будет, если сделать их
    деревьями?»: что произойдёт и какой просьбой это сделать."""
    what = f"выделенное ({count_phrase(selection.count, 'object')})" if selection and selection.count else "выделенное"
    if operation == "delete":
        return f"Если удалить {what}, оно исчезнет из плана — вернуть можно Ctrl+Z. Чтобы удалить, скажите «удали выделенные»."
    if operation == "to_tree":
        return (
            f"Если сделать {what} деревьями, посадки останутся на местах, но отступы проверятся по нормам для деревьев — "
            "они больше, чем у кустов, так что могут появиться нарушения; вернуть можно Ctrl+Z. "
            "Чтобы поменять тип, скажите «сделай их деревьями»."
        )
    return (
        f"Если сделать {what} кустами, посадки останутся на местах, а отступы проверятся по нормам для кустарника; "
        "вернуть можно Ctrl+Z. Чтобы поменять тип, скажите «сделай их кустами»."
    )


def _describe_select(command: SelectCommand, facts: PlanFacts | None, violations_count: int | None) -> str:
    if command.target == "none":
        return "Сняла выделение."
    if command.target == "violations":
        return f"Выделила {count_phrase(violations_count, 'violation')}." if violations_count else "Выделила посадки с нарушениями."
    if command.target == "all":
        total = (sum(facts.counts.values()) or facts.item_count) if facts else 0
        return f"Выделила всё ({count_phrase(total, 'object')})." if total else "Выделила всё."
    planting_type = {"trees": "tree", "shrubs": "shrub", "lawn": "lawn"}[command.target]
    if command.species:
        n = dict(facts.species(planting_type)).get(command.species, 0) if facts else 0
        tail = f" ({count_phrase(n, planting_type)})" if n else ""
        return f"Выделила породу «{command.species}»{tail}."
    n = facts.count(planting_type) if facts else 0
    if planting_type == "lawn":
        return f"Выделила весь газон ({count_phrase(n, 'plot')})." if n else "Выделила газон."
    return f"Выделила все {_NOM[planting_type]} ({fmt_int(n)})." if n else f"Выделила все {_NOM[planting_type]}."


def describe_actions(
    commands: list[AssistantCommand],
    dropped: list[Dropped],
    *,
    current: AssistantGenerationSettings,
    selection: AssistantSelection | None,
    violations_count: int | None,
    facts: PlanFacts | None,
    default_spacing: dict[str, float],
) -> str:
    """Что сделано (и что нет — с объяснением), по порядку команд."""
    sentences: list[str] = []
    select_at: int | None = None
    has_select = any(isinstance(c, SelectCommand) for c in commands)
    for command in commands:
        if isinstance(command, SettingsCommand):
            sentences.append(describe_settings(current, command.settings, default_spacing))
        elif isinstance(command, EditSelectionCommand):
            text = _EDIT_VERB[command.operation]
            if selection and selection.count and not has_select:
                text += f" ({count_phrase(selection.count, 'object')})"
            sentences.append(text + " — отменить можно Ctrl+Z.")
        elif isinstance(command, SelectCommand):
            sentences.append(_describe_select(command, facts, violations_count))
            if command.target != "none":
                select_at = len(sentences) - 1
        elif isinstance(command, UiCommand):
            sentences.append(_UI_TEXT[command.command])
    confirmed = False
    for drop in dropped:
        if drop.reason in ("after_select", "nothing_selected") and select_at is not None:
            if not confirmed:
                sentences[select_at] = sentences[select_at].rstrip(".") + f" — {_CONFIRM[drop.operation]}."
                confirmed = True
        elif drop.reason in ("after_select", "nothing_selected"):
            sentences.append(
                "Сейчас на карте ничего не выделено — выделите объекты кликом или рамкой (или скажите, например, "
                f"«выдели все кусты»), и я их {_WILL_DO[drop.operation]}."
            )
        elif drop.reason == "select_failed":
            sentences.append(_NOT_DONE[drop.operation])
        elif drop.reason == "with_deselect":
            sentences.append(
                f"Не поняла: {_WHICH_ONE[drop.operation]} или только снять выделение? Выделено "
                f"{count_phrase(selection.count if selection else 0, 'object')} — скажите «{_SAY[drop.operation]}» "
                "или «сними выделение»."
            )
        elif drop.reason == "asked":
            sentences.append(_asked_text(drop.operation, selection))
        elif drop.reason == "lawn_only":
            sentences.append("Газон — это площадь, а не точка: деревом или кустом его не сделать.")
        elif drop.reason == "same_type":
            sentences.append(f"Выделенные и так {'деревья' if drop.operation == 'to_tree' else 'кусты'}.")
    return " ".join(dict.fromkeys(sentences))


# --- Нормализация и части сообщения -------------------------------------------


# Опечатки в словах, от которых зависит разбор: «кусиы», «дерьвья», «газно»,
# «сколко». Без поправки слово просто не находится, и просьба применяется не к
# тому («кусиы пореже» разреживало и деревья) или не понимается вовсе.
# Правится слово от 5 букв, отстоящее от слова ниже на одну правку (замена,
# пропуск, лишняя буква, перестановка соседних) и с той же первой буквы.
# Настоящие слова-соседи не трогаются: «деревня» — не «деревья», «газом» — не
# «газон», «столько» — не «сколько».
#
# Глаголов удаления здесь нет и быть не должно: поправка не вправе породить
# разрушительную команду. «Спасибо, удачи!» становилось «спасибо, удали!» и
# удаляло выделенное — «удачи» от «удали» на одну букву, как и «удалы»,
# «удила», «удалишь». Удаление — только слово, написанное без ошибки
# (проверяет тест на всех целях поправки).
_TYPO_TARGETS = (
    "деревья", "деревьев", "деревьями", "деревьям", "деревьях", "дерево", "дерева", "деревом", "дереву", "деревце",
    "кусты", "кустов", "кустами", "кустам", "кустах", "куста", "кусту", "кустом", "кусте", "кустарник", "кустарники",
    "кустарников", "кустарником", "кустарниками", "кустарника", "кустики", "кустиков",
    "газон", "газона", "газоном", "газону", "газоны", "газоне", "газонов",
    "сколько", "пореже", "погуще", "почаще", "плотнее", "поплотнее", "выдели", "выделите", "выделить", "выбери",
    "покажи", "покажите", "интервал", "расстояние", "нарушения", "нарушений",
    "сгенерируй", "перегенерируй", "пересчитай",
)
_TYPO_KNOWN = frozenset(_TYPO_TARGETS) | {
    "столько", "почище", "порежь", "порежу", "покажу", "выдали", "выделю", "выделил", "выберу", "выделит",
    "пересчитал", "газом", "плотное", "плотные",
}
# Начинается так — уже узнаётся (или это другое слово: «деревня», «газовый», «кусок»).
_TYPO_KNOWN_PREFIXES = ("дерев", "куст", "газон", "газов", "куск", "кусок", "дешев")


def _one_edit_apart(a: str, b: str) -> bool:
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (
            len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
        )
    if abs(len(a) - len(b)) != 1:
        return False
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = next((k for k, (x, y) in enumerate(zip(short, long)) if x != y), len(short))
    return short[i:] == long[i + 1:]


def _fix_typo(match: re.Match) -> str:
    word = match.group(0)
    if word in _TYPO_KNOWN or word.startswith(_TYPO_KNOWN_PREFIXES):
        return word
    return next((t for t in _TYPO_TARGETS if t[0] == word[0] and _one_edit_apart(word, t)), word)


def normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"(?<=\d),(?=\d)", ".", text)  # «2,5 м» -> «2.5 м»
    text = re.sub(r"\b([23])\s*-?\s*[dд]\b", r"\1d", text)  # «3-д», «3 D» -> «3d»
    return re.sub(r"[а-я]{5,}", _fix_typo, text)


_CLAUSE_SPLIT = re.compile(r"[,;:!?\n()«»\"]+|\.(?!\d)|\s(?:и|а|но|плюс|потом|затем|также|а также)\s")


# «Удали деревья, у которых нарушения» — придаточное про те же деревья: отдельно
# от него «удали деревья» убрало бы из рецепта все деревья.
_RELATIVE = re.compile(r"^((у|с|в|на|из|для|под|над|около|возле)\s+)?котор\w*\b")


# Вопрос или условие, а не просьба: «можно удалить выделенные?», «что будет,
# если удалить их?», «а если сделать их кустами», «стоит ли их удалить». Правку
# из такого предложения не выполняет ни один режим (guard_commands, причина
# asked): разбор срезает «?» вместе с пунктуацией, и «можно удалить
# выделенные?» удаляло, а модель может послушаться вопроса буквально.
# Проверяется по предложениям, а не по сообщению: в «удали выделенные. А
# сколько кустов?» удаление — отдельная просьба.
_SENTENCE_BREAK = re.compile(r"(?<=[!?\n])|(?<=\.)(?!\d)")
_ASKING = re.compile(
    r"^(а\s+|и\s+|ну\s+|так\s+)?(можно|нельзя|что будет|что если|что случится|что произойдет|если|стоит|а вдруг)\b"
    r"|\bли\b"
)


def _clause_parts(sentence: str) -> list[str]:
    parts = []
    for part in _CLAUSE_SPLIT.split(f" {sentence} "):
        part = re.sub(r"[^\w\s.]", " ", part)
        part = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", part)
        part = re.sub(r"\s+", " ", part).strip()
        if part:
            parts.append(part)
    return parts


def _marked_clauses(text: str) -> list[tuple[str, bool]]:
    """Части сообщения по порядку и для каждой — из вопроса/условия ли она."""
    clauses: list[tuple[str, bool]] = []
    for sentence in _SENTENCE_BREAK.split(normalize(text)):
        parts = _clause_parts(sentence)
        asked = "?" in sentence or any(_ASKING.search(part) for part in parts)
        for part in parts:
            if clauses and _RELATIVE.match(part):
                previous, previous_asked = clauses[-1]
                clauses[-1] = (previous + " " + part, previous_asked or asked)
            else:
                clauses.append((part, asked))
    return clauses


def split_clauses(text: str) -> list[str]:
    return [clause for clause, _ in _marked_clauses(text)]


def is_question(message: str) -> bool:
    """Есть ли в сообщении вопрос или условие (см. _ASKING) — для модельного
    пути, где неизвестно, из какого предложения пришёл вызов."""
    return any(asked for _, asked in _marked_clauses(message))


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


_SUBJECT = _rx(r"\b(?:(?P<tree>дерев\w*)|(?P<shrub>куст\w*)|(?P<lawn>газон\w*|трав[аыуое]\w*|лужа(йк|ек)\w*))")

# Нормы: «ближе к трубам», «уменьши отступ от дороги», «отключи нормы».
_CLOSER = _rx(
    r"\b(ближе|поближе|вплотную|впритык|поверх|дальше|подальше)\b|\bпрямо\s+(на|над|у|возле)\b"
    r"|\b(уменьш\w*|сократи\w*|убери\w*|меньше|без|сократить)\s+(\w+\s+)?отступ\w*"
    # «Сделай отступ от дороги 1 м» — менять отступ, хоть в какую сторону, нечем.
    r"|\b(сделай|поставь|установи|задай|измени|поменяй|увеличь)\w*\s+(\w+\s+)?отступ\w*"
)
_SETBACK_OBJECT = _rx(
    r"\b(дорог\w*|проезж\w*|труб\w*|сет[ьи]\b|сетей|сетям|сетями|коммуникац\w*|кабел\w*|здани\w*|дом\w*|стен\w*|"
    r"фасад\w*|теплотрасс\w*|теплосет\w*|газопровод\w*|газов\w*|газ|газа|газу|водопровод\w*|канализац\w*|лэп|"
    r"опор\w*|столб\w*|бордюр\w*|"
    r"борт\w*|трамва\w*|отступ\w*)\b"
)
_BETWEEN = _rx(r"\bдруг\b|\bмежду\b")
# Только повелительное «нарушь/нарушай»: «нарушения норм», «нарушены нормы» —
# это вопрос о нарушениях, а не просьба нарушить.
_NORMS_OFF = _rx(
    r"\b(отключи\w*|выключи\w*|игнорир\w*|не соблюда\w*|нарушь\w*|нарушай\w*|нарушать|нарушить|ослаб\w*|убери\w*"
    r"|отмени\w*|без)\s+(\w+\s+)?норм(ы|у|ам|ами|ах|атив\w*)?\b"
)
# «Проверь план на нормы», «все ли по нормам?» — вопрос о нарушениях.
_NORMS_CHECK = _rx(r"\b(провер\w*|все ли|соответстви\w*|соответству\w*|соблюден\w*|соблюдаются|в норме|по норм(ам|е))\b")
_NORMS_WORD = _rx(r"\bнорм(ы|у|ам|ами|ах|е|атив\w*)?\b|\bотступ\w*|\bнарушен\w*|\bтребовани\w*|\bплан\w*")

# Вопросы.
_WHY = _rx(r"\b(почему|отчего|откуда|зачем|с чего|по какой причине)\b|\bв чем (причина|дело)\b")
# «Мало деревьев, почему?» — хвостовой вопрос к предыдущей части, не отдельная просьба.
_BARE_WHY = _rx(r"^(а\s+|и\s+|ну\s+|так\s+)?(почему|отчего|зачем|с чего бы|в чем (причина|дело)|как так)(\s+(так|это|же))?$")
_FEW = _rx(
    r"\b(мало|маловато|нет|не хватает|меньше|пусто|исчезл\w*|пропал\w*)\b"
    r"|\bне\s+(сажа\w*|посади\w*|посажен\w*|сади\w*|стоят|видно)"
)
_FEW_STATEMENT = _rx(
    r"\b(мало|маловато|не хватает|почти нет|совсем нет)\b|\bнет\s+(дерев|куст|газон)"
    r"|\bне\s+(посажен\w*|сажа(ют|ются|ет|ешь|ла|ли)\b)|\bпочти не\b"
)
_ONLY_SUBJECT = _rx(r"\b(только|лишь|одни|одна|одну)\s+(дерев|куст|газон|трав)")
_DEICTIC = _rx(
    r"\b(здесь|тут|сюда|там|это|эта|этот|эту|этой|этого|она|он|ее|его|выделенн\w*|выбранн\w*|именно|близко|рядом)\b"
    r"|\bэто место\b"
)
_JUSTIFY = _rx(r"\bобосну\w*|\bобосновани\w*|\bна каком основании\b|\bпо какой норме\b")
_PLANTING_WORDS = _rx(r"\b(посад\w*|зелен\w*|растени\w*|озеленени\w*)")
_HOW_MANY = _rx(r"\b(сколько|скока|скоко)\b")
# «Количество деревьев» — вопрос, «уменьши количество кустов» — просьба.
_COUNT_NOUN = _rx(r"\bкол\s*во\b|\bколичеств\w*|\bчисло\s+(дерев|куст|газон|посад|пород|нарушен|объект)")
_CHANGE = _rx(
    r"\b(увелич\w*|уменьш\w*|сократ\w*|больше|побольше|меньше|поменьше|добав\w*|прибав\w*|убав\w*|сниз\w*|повыс\w*)\b"
)
# «Сколько места под деревья», «какая площадь газона».
_AREA_Q = _rx(r"\b(мест[ао]|площад\w*|пространств\w*|квадрат\w*|м2|м²|кв м\w*)\b")
_SPECIES_WORDS = _rx(r"\b(пород\w*|сорт\w*|видов)\b")
_WHICH = _rx(r"\b(какие|какая|какой|каких|что за|список|перечисли\w*|назови\w*)\b")
_SETTINGS_Q = _rx(
    r"\b(какой|какая|какие|каков\w*)\s+(сейчас\s+|у тебя\s+|у нас\s+)?(интервал\w*|шаг\w*|режим\w*|настройк\w*|параметр\w*)"
    r"|\b(текущие|сейчас)\s+(настройк\w*|параметр\w*)|\bсколько\s+рядов\b|^(настройки|параметры)$"
    r"|\b(покажи|какие|назови)\s+(\w+\s+)?(настройк\w*|параметр\w*)"
)
_SUMMARY = _rx(
    r"\b(сводк\w*|статистик\w*|итог\w*|обзор\w*)\b"
    r"|\bчто\s+(ты\s+|вы\s+|у\s+(тебя|нас)\s+|там\s+)?(в плане|получилось|посажено|посадила|сделала|построила)"
    r"|\b(опиши|расскажи)\s+(о\s+|про\s+)?план\w*"
)
_SELECTED_WORD = _rx(r"\bвыделен\w*|\bвыбран\w*")

# Действия.
_DESELECT = _rx(
    r"\b(сними|снять|убери|убрать|сбрось|сбросить|очисти|очистить|отмени|отменить)\s+(\w+\s+)?"
    r"(выделени\w*|выбор\w*|подсветк\w*)"
    r"|\b(сними|снять|сбрось|сбросить|очисти|очистить)\s+(\w+\s+)?(выделенн\w*|выбранн\w*)"
    r"|\bничего не выделяй|\bразвыдел\w*"
)
# Отменить правку может только сам интерфейс (Ctrl+Z) — команды для этого нет.
_UNDO = _rx(
    r"\b(отмени\w*|откати\w*|верни\w*)\s+(\w+\s+)?(последн\w*|действи\w*|правк\w*|удалени\w*|изменени\w*|как было)"
    r"|^(отмени|отменить|откати|откатить|undo)$|\bкак было\b|\bctrl\s*z\b"
)
_VIOLATION = _rx(r"\bнаруш\w*|\bнесоответств\w*")
_REPORT = _rx(r"\bотчет\w*|\bjson\b|\bджейсон\w*|\bcsv\b|\bцсв\b|\bexcel\b|\b[эе]ксел\w*|\bтаблиц\w*")
_CSV = _rx(r"\bcsv\b|\bцсв\b|\bexcel\b|\b[эе]ксел\w*|\bтаблиц\w*")
_DOWNLOAD = _rx(r"\b(скача\w*|скачать|выгруз\w*|сохрани\w*|экспорт\w*|сформируй\w*|выдай|отдай|загрузи\w*)\b")
_DXF = _rx(r"\bdxf\b|\bдхф\b|\bчертеж\w*|\bавтокад\w*|\bautocad\b|\bcad\b|\bподоснов\w*|\bэкспорт\w*|\bвыгруз\w*")
_PLAN_FILE = _rx(r"\b(план\w*|файл\w*|результат\w*|проект\w*)\b")
_VIEW3D = _rx(r"\b3d\b|\bобъем\w*|\bтрехмер\w*")
_VIEW2D = _rx(r"\b2d\b|\bдвухмер\w*|\bдвумер\w*|\bплоск\w*|\bна карту\b|\bк карте\b|\bкарту\b|\bобычн\w* вид\w*")
# «Убери 3D», «закрой 3D», «выйди из 3D» — назад на карту, а не открыть 3D.
_VIEW3D_OFF = _rx(
    r"\b(убери\w*|убрать|выключи\w*|отключи\w*|закрой\w*|закрыть|скрой\w*|выйди\w*|выйти|хватит|без)\s+(\w+\s+)?3d\b"
    r"|\bиз\s+3d\b|\bне\s+(надо|нужно)\s+(\w+\s+)?3d\b"
)
# Только целиком: «вернись к эвристике» — про режим, а не про карту.
_BACK = _rx(r"^(давай\s+)?(вернись|вернуться|вернемся|назад)(\s+(назад|обратно))?$")

# (?!л) в глаголах ниже — прошедшее время: «я удалил лишние кусты, пересчитай»
# и «я выделил деревья, удали их» рассказывают, что сделано, а не просят.
# (?!т\b) — будущее: «это удалит выделенные?» — вопрос, а не просьба.
_DELETE = _rx(r"\b(удали(?!л|т\b|тся)\w*|убери|убрать|уберите|сотри|стереть|снеси|снести|выкинь|выбрось|вырежи|удаляй)\b")
# Глаголы правки карты, а не рецепта: «удали (все) кусты», «сотри липы» —
# выделить группу и спросить (как у модели в промпте), а не перестроить план
# без кустов. «Убери кусты», «без кустов», «убери кусты из плана» — рецепт:
# «убери» — то же, что «без», а пересчёт без типа не трогает остальные типы.
_ERASE = _rx(r"\b(удали(?!л|т\b|тся)\w*|сотри|стереть|снеси|снести|вырежи|удаляй)\b")
_FROM_RECIPE = _rx(r"\bиз\s+(плана|рецепта|генераци\w*|настроек)\b")
# Слова, которые не меняют смысла голого «удали»: «удали пожалуйста», «ну удаляй».
_FILLER = _rx(
    r"\b(пожалуйста|плиз|пж|пжлст|давай\w*|юна|ну|же|ка|уже|сейчас|теперь|тогда|скорее|быстро|быстрее|можешь|можно|ты"
    r"|мне|просто)\b"
)
# «Не удаляй выделенные», «не делай их деревьями» — отказ от правки, а не правка.
_NEG_DELETE = _rx(
    r"\bне\s+(надо\s+|нужно\s+|стоит\s+)?(\w+\s+)?(удал\w*|убира\w*|убер\w*|убрать|стира\w*|сотр\w*|снос\w*|снес\w*"
    r"|выкид\w*|выкин\w*|выбрас\w*|трога\w*|трон\w*)"
)
_NEG_RETYPE = _rx(
    r"\bне\s+(надо\s+|нужно\s+|стоит\s+)?(\w+\s+)?(дела\w*|сдела\w*|превра\w*|меня\w*|поменя\w*|замен\w*|передел\w*"
    r"|перевод\w*|переве\w*)"
)
# «Удали кусты у дороги» — выбор по месту. Такого инструмента нет; молча
# убрать из рецепта все кусты вместо нескольких у дороги — хуже, чем честно
# попросить выделить их рамкой.
_PLACE = _rx(
    r"\b(у|возле|около|вдоль|напротив|перед|позади|вокруг|посреди|рядом\s+с|недалеко\s+от|поблизости\s+от|по\s+краю"
    r"|на\s+углу|в\s+углу|в\s+начале|в\s+конце|слева\s+от|справа\s+от)\s+"
    r"(?!нас\b|меня\b|тебя\b|вас\b|них\b|него\b|нее\b|всех\b|которых\b|которого\b|которой\b|каждого\b)\w+"
    r"|\b(слева|справа|севернее|южнее|восточнее|западнее|на\s+(севере|юге|востоке|западе))\b"
)
_SEL_REF = _rx(r"\b(выделенн\w*|выбранн\w*|отмеченн\w*|их|это|эти|этот|эту|эта|его|ее|оба|обе)\b")
# Ссылка именно на выделение, а не местоимение: «их» после «сколько кустов?» — это кусты.
_SELECTED_REF = _rx(r"\b(выделенн\w*|выбранн\w*|отмеченн\w*)\b")
_SINGLE_ITEM = _rx(r"\b(дерево|деревце|куст|кустик)\b")
_RETYPE_VERB = _rx(
    r"\b(сделай\w*|сделать|преврати(?!л)\w*|замени(?!л)\w*|поменяй\w*|переделай\w*|смени(?!л)\w*|переведи\w*"
    r"|конвертир\w*|пусть)\b"
)
_PAST_RETYPE = _rx(r"\b(превратил|заменил|сменил|сделал|поменял|перевел|переделал)\w*")
_TO_TREE = _rx(r"(?<!\bс )\b(деревьями|деревом|деревцем)\b|\b(в|на)\s+дерев\w*")
_TO_SHRUB = _rx(r"(?<!\bс )\b(кустами|кустом|кустарником|кустарниками|кустиками|кустиком)\b|\b(в|на)\s+куст\w*")
_TO_PREP = _rx(r"\b(в|на)\s+(дерев|куст)")
_ALL_WORD = _rx(r"\b(все|весь|всех|целиком)\b")
_OBJECTS = _rx(r"\b(объект\w*|посадк\w*|растени\w*)\b")
_SELECT_VERB = _rx(
    r"\b(выдели(?!л|т\b|тся)\w*|выделя\w*|выбери\w*|выбрать|отметь\w*|отметить|подсвети(?!л)\w*|покажи\w*|показать"
    r"|найди\w*|найти)\b"
)
_STRICT_SELECT_VERB = _rx(r"\b(выдели(?!л|т\b|тся)\w*|выделя\w*|выбери\w*|выбрать|отметь\w*|отметить|подсвети(?!л)\w*)\b")
_WHERE = _rx(r"\bгде\b")

_NEG = _rx(
    r"\b(не|без|убери\w*|убрать|удали(?!л)\w*|отключи(?!л)\w*|выключи(?!л)\w*|хватит|отмени\w*|нельзя|запрет\w*"
    r"|запрещ\w*)\b"
)
_ROWS_TWO = _rx(
    r"\b(двумя|два|2|двух|двойн\w*)\s+ряд\w*|\bдвух\s*рядн\w*|\bдвурядн\w*|\b2\s*х?\s*рядн\w*|\bвтор\w*\s+ряд\w*"
    r"|\bдвойн\w*\s+алле\w*"
)
_ROWS_ONE = _rx(r"\b(одним|один|1|одного|одинарн\w*)\s+ряд\w*|\bодно\s*рядн\w*|\bодинарн\w*|\b1\s*рядн\w*")
_PAVING = _rx(r"\b(тротуар\w*|покрыти\w*|асфальт\w*|плитк\w*|плиточн\w*|мощен\w*|мощени\w*|решетк\w*|брусчатк\w*)")
_PAVING_EXCLUDE = _rx(r"\bкрон\w*|\bвдоль\b")
_HEURISTIC = _rx(
    r"\bэвристик\w*|\bпо правилам\b|\bбез\s+(ml|мл|модел\w*|нейро\w*|машинн\w*)"
    r"|\b(выключи|отключи|убери|отмени)(?!л)\w*\s+(режим\w*\s+)?(ml|мл|ии|модел\w*|нейро\w*)"
    r"|\b(обычн|прост|стандартн|классическ)\w*\s+(режим|оценк|скоринг)\w*"
)
# «ИИ» — только рядом с «режим»: «ты ИИ?» — вопрос о Юне, не переключение.
_ML = _rx(
    r"\bml\b|\bмл\b|\bмашинн\w*|\bнейро\w*|\bмодел\w*|\bпроектировщик\w*|\bобучен\w*"
    r"|\bрежим\w*\s+(ии|ai)\b|\b(ии|ai)\s+режим\w*"
)
_REGEN = _rx(
    r"\b(с?генерир\w*|сгенери\w*|перегенер\w*|построй|постройте|построить|перестро(?!ил)\w*|пересчита(?!л)\w*|пересобер\w*"
    r"|пересобрать|regenerate|generate|rebuild)\b"
    r"|\b(обнови\w*|запусти\w*|сделай|создай|новый|другой)\s+(\w+\s+)?(план\w*|генерац\w*|вариант\w*)"
    r"|\bзаново\b|\bеще раз\b|\bеще\s+(один\s+)?вариант\w*"
)
_ONLY_TYPES = _rx(r"\b(только|лишь|одни|одну|исключительно)\s+(\w+\s+)?(дерев\w*|куст\w*|газон\w*|трав\w*)")
# «Оставь деревья и газон» — то ли «только они», то ли «их не трогай» (см.
# _apply_types); «оставь кусты как есть» — не трогать.
_KEEP = _rx(r"\b(оставь\w*|оставить)\s+(\w+\s+)?(дерев\w*|куст\w*|газон\w*|трав\w*)")
_AS_IS = _rx(r"\bкак (есть|было)\b|\bв покое\b|\bбез изменени\w*|\bкак сейчас\b")
_REMOVE = _rx(
    r"\b(убери\w*|убрать|удали(?!л)\w*|удалить|без|исключи\w*|выключи(?!л)\w*|отключи(?!л)\w*|выкинь\w*|не надо"
    r"|не нужно|не нужен|не нужна|не нужны|не сажай\w*|не сажать|не сади\w*|не хочу|не добавляй\w*)\b"
)
_ADD = _rx(
    r"\b(добавь\w*|добавить|верни\w*|вернуть|включи(?!л)\w*|включить|нужен|нужна|нужно|нужны|хочу|давай|сажай\w*"
    r"|посади(?!л)\w*|посадить|засади(?!л)\w*)\b|\bс\s+(деревьями|кустами|кустарником|кустарниками|газоном|травой)\b"
)
_ALL_TYPES_RX = _rx(r"\bвсе\s+(типы|виды)\b|\bвсе вместе\b|\bвсего понемногу\b")
# Только глаголы посадки: «давай всё заново» — это перегенерация, а не все типы.
_PLANT_ALL = _rx(r"\b(посади(?!л)\w*|сажай\w*|засади(?!л)\w*|добавь\w*|включи(?!л)\w*|верни\w*)\s+(\w+\s+)?все\b")
_GEN_PLURAL = _rx(r"\b(деревьев|кустов|кустарников|кустиков|посадок)\b")
# «Столько кустов не нужно» — меньше кустов, а не «без кустов».
_NOT_SO_MANY = _rx(
    r"\b(столько|так много)\s+(\w+\s+)?не\s+(нужно|надо)\b|\bне\s+(нужно|надо)\s+(\w+\s+)?(столько|так много)\b"
)

_SPARSER = _rx(
    r"\b(реже|пореже|разреди(?!л)\w*|разреж\w*|прореди(?!л)\w*|подальше|дальше|просторн\w*|свободн\w*|шире|пошире|пожиже)\b"
    r"|\bне так\s+(густо|плотно|часто|тесно)"
    # Оценка, а не просьба, но смысл тот же: «кустов многовато» — реже.
    r"|\bслишком\s+(много|густ\w*|плотн\w*|част\w*|тесн\w*)\b|\b(многовато|густовато|тесновато|перебор)\b"
)
_DENSER = _rx(
    r"\b(чаще|почаще|гуще|погуще|плотнее|поплотнее|ближе|поближе|теснее|потеснее|кучнее|сгусти(?!л)\w*|уплотни(?!л)\w*"
    r"|загусти(?!л)\w*)\b|\bслишком\s+(редко|разреженно|просторно)\b|\b(редковато|реденько)\b"
)
_INTERVAL = _rx(r"\b(интервал\w*|шаг\w*|расстояни\w*|промежут\w*|дистанц\w*|зазор\w*)\b|\bотступ\w*\s+между\b")
_EVERY = _rx(r"\b(через|кажды\w*)\b")
_INCREASE = _rx(r"\b(увелич\w*|больше|побольше|шире|расширь\w*|длиннее)\b")
_DECREASE = _rx(r"\b(уменьш\w*|меньше|поменьше|сократи\w*|сузь\w*|короче)\b")
_MORE = _rx(r"\b(больше|побольше|добавь\w*|прибавь\w*|увелич\w*)\b")
_LESS = _rx(r"\b(меньше|поменьше|убавь\w*|сократи\w*|уменьш\w*)\b")
_SLIGHTLY = _rx(r"\b(чуть|немного|немножко|слегка|капельку)\b")
_MULTIPLIER = _rx(r"\bв\s+(\d+(?:\.\d+)?|два|три|полтора)\s+раз\w*|\b(вдвое|втрое)\b")
_NUMBER = r"(\d+(?:\.\d+)?)(?![\d.])"
_BARE_NUMBER = _rx(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_NOT_METRES = _rx(r"%|\bпроцент\w*|\bраз\w*|\bшт\w*|\bштук\w*|\bряд\w*")
_METRES = _rx(rf"(?<![\w.]){_NUMBER}\s*(?:м|метр\w*|m)\b")
_DELTA = _rx(rf"\bна\s+{_NUMBER}\s*(?:м|метр\w*|m)\b")
_AFTER_INTERVAL = _rx(
    rf"(?:интервал\w*|шаг\w*|расстояни\w*|промежут\w*|дистанц\w*|через|кажды\w*)\D{{0,30}}?{_NUMBER}"
    r"(?!\s*(?:раз|ряд|d\b|%|процент|шт|штук))"
)
_WORD_NUMBERS = {
    "один": 1, "одного": 1, "полтора": 1.5, "полутора": 1.5, "два": 2, "двух": 2, "три": 3, "трех": 3,
    "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
    "двенадцать": 12, "двенадцати": 12, "пятнадцать": 15, "пятнадцати": 15,
}
_WORD_METRES = _rx(r"\b(" + "|".join(_WORD_NUMBERS) + r")(\s+с\s+половиной)?\s+(?:м|метр\w*)\b")

_HELP = _rx(
    r"\b(помощь|помоги\w*|help|умеешь|можешь|команды|команд|пользоваться|возможност\w*|функци\w*)\b|\bкто ты\b"
    r"|\bты кто\b|\bкто такая\b|\bчто ты такое\b"
)
_GREETING = _rx(r"\b(привет\w*|здравствуй\w*|здрасьте|хай|hello|hi|салют)\b|\bдобр\w*\s+(день|утро|вечер)\b")
_THANKS = _rx(r"\b(спасибо|спс|благодарю|мерси|thanks|супер|отлично|класс)\b")
_BYE = _rx(r"\b(удачи|до свидания|до встречи|всего доброго|всего хорошего)\b|^(ну\s+)?пока$")


# --- Разбор частей ------------------------------------------------------------


@dataclass
class _Intent:
    kind: str
    subjects: list[str] = field(default_factory=list)
    value: object = None
    extra: dict = field(default_factory=dict)
    # Принимает ли подлежащее из соседней части («без газона и кустов»).
    takes_subjects: bool = False


@dataclass
class _Ctx:
    settings: AssistantGenerationSettings
    facts: PlanFacts | None
    selection: AssistantSelection | None
    violations_count: int | None
    view: str
    default_spacing: dict[str, float]

    @property
    def selected_count(self) -> int:
        return self.selection.count if self.selection else 0


def _subjects(text: str) -> list[str]:
    found: list[str] = []
    for match in _SUBJECT.finditer(text):
        planting_type = next(name for name in ALL_TYPES if match.group(name))
        if planting_type not in found:
            found.append(planting_type)
    return found


def _number(text: str) -> float | None:
    """Метры, названные явно: «6 м», «2.5 метра», «через три метра», «интервал 4»."""
    match = _METRES.search(text) or _AFTER_INTERVAL.search(text)
    if match:
        return float(match.group(1))
    match = _WORD_METRES.search(text)
    if match:
        return _WORD_NUMBERS[match.group(1)] + (0.5 if match.group(2) else 0.0)
    return None


def _multiplier(text: str) -> float | None:
    match = _MULTIPLIER.search(text)
    if not match:
        return None
    word = match.group(1) or match.group(2)
    value = {"два": 2.0, "три": 3.0, "полтора": 1.5, "вдвое": 2.0, "втрое": 3.0}.get(word)
    value = value if value is not None else float(word)
    return value if value > 1 else None


def _why(text: str, subjects: list[str], ctx: _Ctx) -> _Intent:
    if _VIOLATION.search(text) and not _DEICTIC.search(text):
        return _Intent("why_violations")
    if _ONLY_SUBJECT.search(text):
        return _Intent("why_few", subjects=subjects, extra={"inverted": True}, takes_subjects=True)
    if _FEW.search(text):
        return _Intent("why_few", subjects=subjects, takes_subjects=True)
    if _DEICTIC.search(text) or _SINGLE_ITEM.search(text):
        return _Intent("why_here")
    if subjects or _PLANTING_WORDS.search(text):
        return _Intent("why_few", subjects=subjects, takes_subjects=True)
    if ctx.facts is not None and ctx.facts.selected is not None:
        return _Intent("why_here")
    return _Intent("why_few", takes_subjects=True)


def _question(text: str, subjects: list[str], ctx: _Ctx) -> list[_Intent] | None:
    if _JUSTIFY.search(text) and not (_DOWNLOAD.search(text) or _REPORT.search(text)):
        return [_Intent("why_here")]
    norms_check = _NORMS_CHECK.search(text) and _NORMS_WORD.search(text)
    # «Проверь, сколько деревьев в плане» — вопрос о числе, а не о нормах.
    if norms_check and not (_WHY.search(text) or _HOW_MANY.search(text) or _COUNT_NOUN.search(text)):
        # «Соответствует ли эта посадка нормам?» — про выделенную; «все ли по нормам?» — про план.
        if _DEICTIC.search(text) or _SINGLE_ITEM.search(text):
            return [_Intent("why_here")]
        return [_Intent("violations_status")]
    if _WHY.search(text):
        intent = _why(text, subjects, ctx)
        if _BARE_WHY.search(text):
            intent.extra["bare"] = True  # «мало деревьев, почему?» — вопрос уже задан соседней частью
        return [intent]
    if _SETTINGS_Q.search(text):
        return [_Intent("settings_show")]
    if _AREA_Q.search(text) and (_HOW_MANY.search(text) or _WHICH.search(text) or text.startswith("площад")):
        # Газон — это и есть площадь: «какая площадь газона» — сколько его в плане.
        if subjects == ["lawn"]:
            return [_Intent("count", subjects=["lawn"], takes_subjects=True)]
        return [_Intent("budget", subjects=subjects, takes_subjects=True)]
    if _HOW_MANY.search(text) or (_COUNT_NOUN.search(text) and not _CHANGE.search(text)):
        if _VIOLATION.search(text):
            return [_Intent("count", value="violations")]
        if _SELECTED_WORD.search(text):
            return [_Intent("count", value="selection")]
        if _SPECIES_WORDS.search(text):
            return [_Intent("count", subjects=subjects, value="species", takes_subjects=True)]
        named = _mentioned_species(text, ctx.facts)
        if named:
            return [_Intent("count", value="species_named", extra={"named": named})]
        absent = None if subjects else _catalogue_species(text)
        if absent:
            return [_Intent("species_absent", value=absent)]
        if subjects:
            return [_Intent("count", subjects=subjects, takes_subjects=True)]
        return [_Intent("count", value="total")]
    if _WHICH.search(text) and (_SPECIES_WORDS.search(text) or subjects) and not _VIOLATION.search(text):
        return [_Intent("species_list", subjects=subjects, takes_subjects=True)]
    if _SUMMARY.search(text):
        return [_Intent("summary")]
    if subjects and _FEW_STATEMENT.search(text):
        return [_Intent("why_few", subjects=subjects, takes_subjects=True)]
    return None


def _ui(text: str, subjects: list[str], ctx: _Ctx) -> list[_Intent]:
    found = []
    if _REPORT.search(text) or (_JUSTIFY.search(text) and _DOWNLOAD.search(text)):
        found.append(_Intent("ui", value="report_csv" if _CSV.search(text) else "report_json"))
    elif _DXF.search(text) or (_DOWNLOAD.search(text) and _PLAN_FILE.search(text)):
        found.append(_Intent("ui", value="export_dxf"))
    leave_3d = _VIEW3D_OFF.search(text)
    if _VIEW3D.search(text) and not leave_3d:
        found.append(_Intent("ui", value="show_3d"))
    elif leave_3d or _VIEW2D.search(text) or (_BACK.search(text) and not subjects):
        found.append(_Intent("ui", value="show_2d"))
    return found


def _retype_operation(text: str) -> str | None:
    # «Расстояние между кустами 2 м», «между деревьями пусть будет 6 м» — это
    # интервал: смена типа не бывает с числом или «между». «Я превратил их в
    # кусты» — рассказ о сделанном.
    if _INTERVAL.search(text) or _BETWEEN.search(text) or _number(text) is not None or _PAST_RETYPE.search(text):
        return None
    to_tree, to_shrub = _TO_TREE.search(text), _TO_SHRUB.search(text)
    return "to_tree" if to_tree and not to_shrub else "to_shrub" if to_shrub and not to_tree else None


def _violation_confirm(text: str) -> dict:
    """«Удали деревья с нарушениями» — выделить нарушения и спросить: правка
    относится к ним, а не к тому, что выделено сейчас."""
    if _NEG_DELETE.search(text) or _NEG_RETYPE.search(text):
        return {}
    if _DELETE.search(text):
        return {"confirm": "delete"}
    operation = _retype_operation(text)
    if operation and (_RETYPE_VERB.search(text) or _TO_PREP.search(text)):
        return {"confirm": operation}
    return {}


def _edit(text: str, subjects: list[str], ctx: _Ctx) -> list[_Intent]:
    operation = _retype_operation(text)
    if _NEG_DELETE.search(text) or (operation and _NEG_RETYPE.search(text)):
        return [_Intent("edit_declined")]
    place = _PLACE.search(text)
    if operation and (_RETYPE_VERB.search(text) or _SEL_REF.search(text) or _TO_PREP.search(text)):
        target_type = "tree" if operation == "to_tree" else "shrub"
        sources = [s for s in subjects if s not in (target_type, "lawn")]
        if sources and not _SEL_REF.search(text):
            if place:
                return [_Intent("spatial", value=operation, extra={"place": place.group(0)})]
            if _ALL_WORD.search(text) or ctx.selected_count == 0:
                # «Сделай все кусты деревьями» — массовая смена типа в два шага:
                # выделить и показать, поменять — по второй просьбе.
                return [_Intent("select", subjects=sources[:1], extra={"confirm": operation})]
        return [_Intent("edit", value=operation, extra={"pronoun": not _SELECTED_REF.search(text)})]
    if _DELETE.search(text) and not _DESELECT.search(text):
        if _SEL_REF.search(text) or _SINGLE_ITEM.search(text):
            pronoun = bool(_SEL_REF.search(text)) and not _SELECTED_REF.search(text)
            return [_Intent("edit", value="delete", extra={"pronoun": pronoun})]
        if place and (subjects or _ALL_WORD.search(text) or _OBJECTS.search(text)):
            return [_Intent("spatial", value="delete", extra={"place": place.group(0)})]
        recipe_words = _PAVING.search(text) or _ROWS_TWO.search(text) or _ROWS_ONE.search(text) or _HEURISTIC.search(text)
        if _ERASE.search(text) and not _FROM_RECIPE.search(text) and not recipe_words:
            # «Удали (все) кусты», «удали липы» — правка карты в два шага, как
            # у модели: выделить и показать, удалить — по второй просьбе. Раньше
            # здесь перестраивался план без кустов: другой смысл, чем в
            # модельном режиме, и ручные правки остальных типов оставались в
            # прежней версии плана.
            named = _mentioned_species(text, ctx.facts)
            if named:
                return [_Intent("select", subjects=[named[0]], extra={"species": named[1], "confirm": "delete"})]
            if subjects:
                return [_Intent("select", subjects=subjects, extra={"confirm": "delete"}, takes_subjects=True)]
            absent = _catalogue_species(text)
            if absent:
                return [_Intent("species_absent", value=absent, extra={"select": True})]
        if subjects or recipe_words:
            return []  # «убери кусты», «убери второй ряд» — это рецепт генерации, не правка
        if _NORMS_OFF.search(text):
            return []
        if _ALL_WORD.search(text):
            return [_Intent("select", value="all", extra={"confirm": "delete"})]
        # Голое «удали (пожалуйста)» — про выделенное. «Убери подсветку»,
        # «удали лишнее» — неизвестно что, и удалять выделенное по догадке нельзя.
        if not _FILLER.sub(" ", _DELETE.sub(" ", text)).strip():
            return [_Intent("edit", value="delete", extra={"pronoun": True})]
    return []


def _select(text: str, subjects: list[str], ctx: _Ctx) -> list[_Intent]:
    verb, where = _SELECT_VERB.search(text), _WHERE.search(text)
    if not verb and not where:
        return []
    place = _PLACE.search(text)
    if place and (subjects or _ALL_WORD.search(text) or _OBJECTS.search(text) or _mentioned_species(text, ctx.facts)):
        # Выделить все деревья вместо тех, что «у дороги», — опасно: следом
        # скажут «удали выделенные», думая, что выделены только они.
        return [_Intent("spatial", value="select", extra={"place": place.group(0)})]
    named = _mentioned_species(text, ctx.facts)
    if named:
        planting_type, names = named
        return [_Intent("select", subjects=[planting_type], extra={"species": names})]
    absent = None if subjects else _catalogue_species(text)
    if absent:
        # Попытка выделить, хоть и неудачная: «выдели дубы и удали их» без
        # дубов не должно оставить голое «удали их» (guard_commands, (b)).
        return [_Intent("species_absent", value=absent, extra={"select": True})]
    if subjects:
        if where and not verb and ctx.facts is not None and all(ctx.facts.count(s) == 0 for s in subjects):
            return [_Intent("why_few", subjects=subjects, takes_subjects=True)]
        return [_Intent("select", subjects=subjects, takes_subjects=True)]
    if verb and (_ALL_WORD.search(text) or _OBJECTS.search(text)):
        return [_Intent("select", value="all")]
    if _STRICT_SELECT_VERB.search(text):
        return [_Intent("select_what")]
    return []


def _rows(text: str) -> int | None:
    if _ROWS_TWO.search(text):
        return 1 if _NEG.search(text) else 2
    if _ROWS_ONE.search(text):
        return 2 if _NEG.search(text) else 1
    return None


def _spacing(text: str, subjects: list[str]) -> list[_Intent]:
    sparser, denser = _SPARSER.search(text), _DENSER.search(text)
    interval = _INTERVAL.search(text)
    up = sparser or (interval and _INCREASE.search(text)) or _NOT_SO_MANY.search(text)
    down = denser or (interval and _DECREASE.search(text))
    delta = _DELTA.search(text)
    if delta and (up or down) and not (up and down):
        step = float(delta.group(1))
        return [_Intent("spacing_delta", subjects=subjects, value=step if up else -step, takes_subjects=True)]
    value = _number(text)
    if value is not None and (interval or subjects or up or down or _EVERY.search(text)):
        return [_Intent("spacing_abs", subjects=subjects, value=value, takes_subjects=True)]
    factor = None
    enable = False
    multiplier = _multiplier(text)
    slight = _SLIGHTLY.search(text)
    if up and not down:
        factor = multiplier or (_SLIGHTLY_SPARSER if slight else SPARSER_FACTOR)
    elif down and not up:
        factor = 1 / multiplier if multiplier else (_SLIGHTLY_DENSER if slight else DENSER_FACTOR)
    elif _GEN_PLURAL.search(text) and _MORE.search(text):
        factor, enable = DENSER_FACTOR, True  # «больше деревьев»: гуще, а выключены — включить
    elif _GEN_PLURAL.search(text) and _LESS.search(text):
        factor = SPARSER_FACTOR
    if factor is None:
        return []
    return [_Intent("spacing_rel", subjects=subjects, value=factor, extra={"enable": enable}, takes_subjects=True)]


def _types(text: str, subjects: list[str], has_spacing: bool) -> list[_Intent]:
    if not subjects:
        if _ALL_TYPES_RX.search(text):
            return [_Intent("types_all")]
        if _PLANT_ALL.search(text) and not has_spacing:
            # «Посади всё: деревья, кусты и газон» — перечень придёт соседними
            # частями; без перечня — все типы.
            return [_Intent("types_add", takes_subjects=True)]
        return []
    if _ONLY_TYPES.search(text) and not has_spacing:
        return [_Intent("types_only", subjects=subjects, takes_subjects=True)]
    if _KEEP.search(text):
        if _AS_IS.search(text):
            return [_Intent("edit_declined")]
        if has_spacing:
            return []
        return [_Intent("types_only", subjects=subjects, extra={"keep": True}, takes_subjects=True)]
    if _REMOVE.search(text) and not _NOT_SO_MANY.search(text):
        return [_Intent("types_remove", subjects=subjects, takes_subjects=True)]
    if _ADD.search(text) and not (has_spacing and _GEN_PLURAL.search(text)):
        return [_Intent("types_add", subjects=subjects, takes_subjects=True)]
    return []


def _settings(text: str, subjects: list[str]) -> list[_Intent]:
    found = []
    rows = _rows(text)
    if rows:
        found.append(_Intent("rows", value=rows))
    paving = None
    if _PAVING.search(text) and not _PAVING_EXCLUDE.search(text):
        paving = not _NEG.search(text)
        found.append(_Intent("paving", subjects=subjects, value=paving, takes_subjects=True))
    # «Покажи 3D-модель» — это вид, а не режим ML.
    ml = [m for m in _ML.finditer(text) if not (_VIEW3D.search(text) and m.group(0).startswith("модел"))]
    if _HEURISTIC.search(text):
        found.append(_Intent("mode", value="heuristic"))
    elif ml and not _WHICH.search(text):  # «какая у тебя модель?» — вопрос, не переключение
        found.append(_Intent("mode", value="ml"))
    spacing = _spacing(text, subjects)
    if rows and not subjects:
        for intent in spacing:  # «двумя рядами через 7 м» — ряды бывают только у деревьев
            intent.subjects = ["tree"]
    if not rows and paving is None:
        found += _types(text, subjects, bool(spacing))
    found += spacing
    if _REGEN.search(text):
        found.append(_Intent("regenerate"))
    return found


def _parse_clause(text: str, ctx: _Ctx) -> tuple[list[str], list[_Intent]]:
    subjects = _subjects(text)
    if (_CLOSER.search(text) and _SETBACK_OBJECT.search(text) and not _BETWEEN.search(text)) or _NORMS_OFF.search(text):
        return subjects, [_Intent("norms")]
    question = _question(text, subjects, ctx)
    if question:
        return subjects, question
    if _DESELECT.search(text):
        return subjects, [_Intent("deselect")]
    if _UNDO.search(text):
        return subjects, [_Intent("undo")]
    violation = _VIOLATION.search(text)
    intents = [_Intent("violations", extra=_violation_confirm(text))] if violation else []
    intents += _ui(text, subjects, ctx)
    # С нарушениями правка — только через выделение нарушений и подтверждение,
    # иначе «удали нарушения» при нуле нарушений удалило бы то, что выделено.
    edit = [] if violation else _edit(text, subjects, ctx)
    if edit:
        intents += edit
    elif not violation:  # «покажи нарушения» — не выделение по типу и не рецепт
        intents += _select(text, subjects, ctx) or _settings(text, subjects)
    if not intents:
        if _HELP.search(text):
            intents.append(_Intent("help"))
        elif _GREETING.search(text):
            intents.append(_Intent("greeting"))
        elif _THANKS.search(text):
            intents.append(_Intent("thanks"))
        elif _BYE.search(text):
            intents.append(_Intent("bye"))
    elif _GREETING.search(text):
        intents.insert(0, _Intent("greeting"))
    return subjects, intents


def _parse(message: str, ctx: _Ctx) -> list[_Intent]:
    """Части по порядку; подлежащее без сказуемого — соседней части."""
    intents: list[_Intent] = []
    pending: list[str] = []
    last_taker: _Intent | None = None
    last_subjects: list[str] = []
    for clause, asked in _marked_clauses(message):
        subjects, found = _parse_clause(clause, ctx)
        for intent in found:
            if intent.kind == "edit":
                intent.extra["asked"] = asked
        if not found and intents and intents[-1].kind.startswith(("spacing", "rows")):
            # «Деревья реже, 8 метров», «интервал деревьев 6 м, кустов 2» —
            # число уточняет предыдущую часть. Без этого «кустов 2» досталось бы
            # предыдущей просьбе как подлежащее, и кусты получили бы 6 м.
            value, bare = _number(clause), _BARE_NUMBER.search(clause)
            if value is None and bare and not _NOT_METRES.search(clause):  # «процентов на 20» — не метры
                value = float(bare.group(1))
            if value is not None:
                previous = intents[-1]
                own = subjects or (["tree"] if previous.kind == "rows" else list(previous.subjects))
                found = [_Intent("spacing_abs", subjects=own, value=value, takes_subjects=True)]
        if not found:
            if subjects and last_taker is not None:
                last_taker.subjects += [s for s in subjects if s not in last_taker.subjects]
            elif subjects:
                pending += [s for s in subjects if s not in pending]
            continue
        for intent in found:
            if intent.takes_subjects:
                if pending:
                    intent.subjects = pending + [s for s in intent.subjects if s not in pending]
                elif not intent.subjects and last_subjects and intent.kind.startswith(("spacing", "paving")):
                    # «кусты пореже, интервал 2 м» — вторая часть о тех же кустах.
                    intent.subjects = list(last_subjects)
                last_taker = intent
        pending = []
        if subjects:
            last_subjects = subjects
        intents += found
    if any(not i.extra.get("bare") and i.kind not in ("greeting", "thanks", "bye", "help") for i in intents):
        intents = [i for i in intents if not i.extra.get("bare")]
    return _refer_to_named_group(intents)


def _named_group(intent: _Intent) -> _Intent | None:
    """Выделение группы, о которой спросили («сколько кустов?», «какие породы
    деревьев?», «сколько нарушений?»), — None, если вопрос о выделенном."""
    if intent.kind == "count" and intent.value == "violations":
        return _Intent("violations")
    if intent.kind == "count" and intent.value == "total":
        return _Intent("select", value="all")
    if intent.kind == "count" and intent.value == "species_named":
        planting_type, names = intent.extra["named"]
        return _Intent("select", subjects=[planting_type], extra={"species": names})
    if intent.kind in ("count", "species_list", "why_few", "budget") and intent.value != "selection" and intent.subjects:
        return _Intent("select", subjects=list(intent.subjects))
    return None


def _refer_to_named_group(intents: list[_Intent]) -> list[_Intent]:
    """«Сколько кустов? Удали их» — «их» это кусты, а не то, что выделено
    сейчас: голое «удали их» удаляло текущее выделение. Правка по
    местоимению после вопроса о группе — та же массовая правка, что «удали
    все кусты»: выделить группу и спросить. «Удали выделенные» — про
    выделенное всегда."""
    resolved: list[_Intent] = []
    group: _Intent | None = None
    for intent in intents:
        if intent.kind == "edit" and intent.extra.get("pronoun") and group is not None:
            extra = {**group.extra, "confirm": intent.value}
            resolved.append(_Intent(group.kind, subjects=list(group.subjects), value=group.value, extra=extra))
            continue
        group = _named_group(intent) or group
        resolved.append(intent)
    return resolved


# --- Ответы на вопросы -------------------------------------------------------


def _violations_status(ctx: _Ctx) -> str:
    if ctx.facts is None:
        return NO_PLAN
    if ctx.violations_count is None:
        return VIOLATIONS_PENDING
    if ctx.violations_count == 0:
        return NO_VIOLATIONS
    return f"В плане {count_phrase(ctx.violations_count, 'violation')} — скажите «покажи нарушения», и я их выделю."


def _why_violations(ctx: _Ctx) -> str:
    """«Почему нарушения?» — причина, только если она известна. Раньше ответ
    всегда винил ручную правку, даже в плане, которого никто не трогал.
    Неизвестна — пусто: число скажет «Выделила …», а причину покажет карточка."""
    manual = ctx.facts.manual_edits if ctx.facts is not None else None
    if manual:
        edited = plural(manual, "изменена", "изменены", "изменены")
        return (
            f"Генератор ставит посадки только по нормам, а {count_phrase(manual, 'planting')} в плане {edited} вручную — "
            "переносом или сменой типа (у дерева отступы больше, чем у куста); нарушения, скорее всего, от этих правок."
        )
    if manual == 0:
        return "Ручных правок в плане нет — значит, нарушения дала сама генерация."
    return ""


def _violations_hint(ctx: _Ctx) -> str:
    if ctx.violations_count == 1:
        return "Какой отступ нарушен — в карточке посадки."
    return "Кликните по любому — в карточке будет, какой отступ нарушен."


def _why_few(intent: _Intent, ctx: _Ctx, hints: bool = True) -> list[str]:
    facts = ctx.facts
    if facts is None:
        return [NO_PLAN]
    if intent.extra.get("inverted"):
        types = [t for t in POINT_TYPES if t not in intent.subjects] or list(POINT_TYPES)
    else:
        types = intent.subjects or list(POINT_TYPES)
    texts: list[str] = []
    tight: list[str] = []
    roomy: list[str] = []
    for planting_type in types:
        n = facts.count(planting_type)
        if planting_type not in ctx.settings.planting_types and n == 0:
            off = "Газон выключен" if planting_type == "lawn" else f"{_cap(_NOM[planting_type])} выключены"
            texts.append(f"{off} в настройках — скажите «добавь {_NOM[planting_type]}».")
            continue
        budget = facts.budget(planting_type)
        area = (
            f"под {_NOM[planting_type]} по нормам остаётся {_area(budget.usable_m2)} из {_area(budget.territory_m2)} "
            f"({_share(budget.usable_share)} % участка)"
            if budget is not None
            else ""
        )
        if not facts.materialized:
            # Счётчиков по типам у схлопнутого плана нет: «деревьев в плане нет» было бы неправдой.
            if not area:
                continue
            text = _cap(area)
        else:
            head = f"В плане {count_phrase(n, planting_type)}" if n else f"{_cap(_GEN[planting_type])} в плане нет"
            if not area:
                texts.append(head + ".")
                continue
            text = f"{head}: {area}"
        top = budget.blockers[:3]
        if top:
            text += ", больше всего закрывают " + ", ".join(f"{b.label} — {round(100 * b.share)} %" for b in top)
        texts.append(text + ".")
        (roomy if budget.usable_share >= _ROOMY_SHARE else tight).append(planting_type)
    if not hints:
        return texts
    # Тесно — дело в нормах, и подсказка про то, что нормы разрешают (покрытие
    # тротуара). Просторно — дело не в площади: у кустов в шаге изгороди, а
    # деревья и так стоят по норме класса кроны — «почаще» их не добавит
    # (tree_floor), добавить может только схема посадки.
    if tight:
        texts.append(f"Отступы — норматив ({NORMS_CITATION}), их не ослабить.")
        if "tree" in tight and not ctx.settings.trees_in_paving:
            texts.append(
                "Можно разрешить деревья в покрытии тротуара с решёткой (743-ПП п. 9.8) — скажите «разреши деревья в тротуаре»."
            )
    if "tree" in roomy:
        floor = tree_floor(ctx.settings.tree_spacing_m)
        offers = _tree_offers(ctx.settings)
        if floor is not None:
            texts.append(
                f"Места под деревья хватает, а интервал сейчас — не меньше {fmt_num(floor)} м: скажите «деревья почаще», "
                "и он станет меньше (но не меньше нормы класса кроны)."
            )
        else:
            texts.append(
                "Места под деревья хватает, но гуще нормы класса кроны их не поставить"
                + (f" — добавить деревья может {' или '.join(offers)}." if offers else ".")
            )
    shrub_step = ctx.settings.shrub_spacing_m or ctx.default_spacing.get("shrub", HEDGE_PITCH_M)
    if "shrub" in roomy and shrub_step > SPACING_LIMITS["shrub"][0]:
        texts.append("Места под кусты хватает — живая изгородь станет гуще с меньшим шагом: скажите «кусты почаще».")
    if "tree" in types and ctx.settings.scoring_mode == "heuristic" and "tree" in ctx.settings.planting_types:
        texts.append("Ещё вариант — «режим ML»: места выбирает модель, обученная на решениях проектировщиков.")
    return texts


def _budget(intent: _Intent, ctx: _Ctx) -> list[str]:
    """«Сколько места под деревья?» — те же цифры, что в «почему мало», без советов."""
    texts = _why_few(intent, ctx, hints=False)
    if ctx.facts is not None and not intent.subjects and ctx.facts.budgets:
        texts.insert(0, f"Площадь участка — {_area(ctx.facts.budgets[0].territory_m2)}.")
    return texts


_SPATIAL_NEXT = {
    "delete": " и скажите «удали выделенные»",
    "to_tree": " и скажите «сделай их деревьями»",
    "to_shrub": " и скажите «сделай их кустами»",
}


def _spatial(intent: _Intent) -> str:
    action = {"delete": "Удалять", "select": "Выделять"}.get(intent.value, "Менять тип")
    return (
        f"{action} по месту («{intent.extra.get('place', '')}») я не умею — выделите нужные посадки на карте "
        f"рамкой (кнопка «Рамка» или клавиша S){_SPATIAL_NEXT.get(intent.value, '')}."
    )


def _cite(act: str, clause: str) -> str:
    short = _short_act(act)
    return f"{short}, {clause}" if clause and clause != "—" else short


def _why_here(ctx: _Ctx) -> str:
    facts = ctx.facts
    if facts is None:
        return NO_PLAN
    selected = facts.selected
    if selected is None:
        if ctx.selected_count > 1:
            return (
                f"Выделено {count_phrase(ctx.selected_count, 'object')} — выберите кликом одну посадку, "
                "и я расскажу, почему она стоит именно там."
            )
        return "Выделите на карте одну посадку (клик по точке) — расскажу, почему она стоит именно там и какие нормы проверены."
    what = _item_name(selected)
    violated = [c for c in selected.checks if not c.satisfied]
    # Заголовки разделов — как в самой карточке (InspectorCard.tsx): у
    # нарушения «Нормативные отступы», иначе «Почему здесь можно». Числа — её же
    # округлением (fmt_m), чтобы ответ и карточка рядом не расходились.
    if violated or not selected.compliant:
        if violated:
            check = violated[0]
            text = (
                f"{what} нарушает нормы: {check.label} — нужно {fmt_m(check.required_m)} м, а здесь "
                f"{fmt_m(check.actual_m)} м ({_cite(check.act, check.clause)})."
            )
        else:
            text = f"{what} нарушает нормы. {selected.summary}"
        if selected.is_manual_edit:
            text += " Посадку правили вручную — перенесите её или удалите («удали выделенные»)."
        else:
            text += " Перенесите её на карте или удалите («удали выделенные»)."
        section = "Нормативные отступы"
    else:
        binding = next((c for c in selected.checks if c.required_m > 0), None)
        if binding is None:
            text = f"{what} стоит по нормам: нормируемых препятствий рядом нет."
        else:
            text = (
                f"{what} стоит по нормам. Ближе всего к пределу — {binding.label}: нужно {fmt_m(binding.required_m)} м, "
                f"здесь {fmt_m(binding.actual_m)} м ({_cite(binding.act, binding.clause)})."
            )
        reason = _first_sentence(selected.ranking_rationale)
        if reason:
            text += f" Место выбрано так: {_lower_first(reason)}"
        section = "Почему здесь можно"
    return text + f" Все отступы — в карточке посадки, раздел «{section}»."


def _item_name(selected) -> str:
    """«Липа мелколистная (дерево)»; у газона породы нет — в плане у него
    species "default", и ответ звучал «default (газон) стоит по нормам»."""
    species = (selected.species or "").strip()
    if species and species.lower() != "default":
        return f"{species} ({_SINGULAR.get(selected.planting_type, selected.planting_type)})"
    return {"lawn": "Этот участок газона", "tree": "Это дерево", "shrub": "Этот куст"}.get(selected.planting_type, "Эта посадка")


def _type_count(planting_type: str, facts: PlanFacts) -> str:
    n = facts.count(planting_type)
    if not n:
        return f"{_cap(_GEN[planting_type])} в плане нет."
    if planting_type == "lawn":
        return f"Газона в плане {count_phrase(n, 'plot')} — {_area(facts.lawn_area_m2)}."
    type_facts = facts.types.get(planting_type)
    schemes = [f"{label} {fmt_int(k)}" for label, k in (type_facts.schemes if type_facts else [])]
    if type_facts and type_facts.loose and schemes:
        schemes.append(f"россыпью {fmt_int(type_facts.loose)}")
    if type_facts and type_facts.in_paving:
        schemes.append(f"в покрытии тротуара {fmt_int(type_facts.in_paving)}")
    tail = f": {', '.join(schemes)}" if schemes else ""
    return f"{_cap(_GEN[planting_type])} в плане {fmt_int(n)}{tail}."


def _totals(facts: PlanFacts) -> str:
    total = sum(facts.counts.values())
    parts = [count_phrase(facts.count(t), t) for t in ALL_TYPES if facts.count(t)]
    return f"В плане {count_phrase(total, 'planting')}: {_join(parts)}." if parts else "План пустой — посадок нет."


def _count(intent: _Intent, ctx: _Ctx) -> str:
    facts = ctx.facts
    if intent.value == "violations":
        return _violations_status(ctx)
    if intent.value == "selection":
        selection = ctx.selection
        if not ctx.selected_count:
            return "Сейчас ничего не выделено."
        parts = [count_phrase(n, t) for t, n in (("tree", selection.trees), ("shrub", selection.shrubs), ("lawn", selection.lawns)) if n]
        return f"Выделено {count_phrase(selection.count, 'object')}" + (f": {_join(parts)}." if parts else ".")
    if facts is None:
        return NO_PLAN
    if not facts.materialized:
        return f"В плане {count_phrase(facts.item_count, 'planting')}."
    if intent.value == "species":
        parts = []
        for planting_type in [t for t in intent.subjects if t in POINT_TYPES] or list(POINT_TYPES):
            species = facts.species(planting_type)
            if species:
                listed = ", ".join(f"{name} {fmt_int(n)}" for name, n in species[:4])
                parts.append(f"у {_GEN[planting_type]} — {count_phrase(len(species), 'species')} ({listed})")
        return _cap("; ".join(parts)) + "." if parts else "Пород в плане нет — деревьев и кустов не посажено."
    if intent.value == "species_named":
        planting_type, names = intent.extra["named"]
        counts = dict(facts.species(planting_type))
        return _cap(", ".join(f"{name} — {count_phrase(counts.get(name, 0), planting_type)}" for name in names)) + "."
    if intent.subjects:
        return " ".join(_type_count(t, facts) for t in intent.subjects)
    return _totals(facts)


def _species_list(intent: _Intent, ctx: _Ctx) -> str:
    facts = ctx.facts
    if facts is None:
        return NO_PLAN
    parts = []
    for planting_type in [t for t in intent.subjects if t in POINT_TYPES] or list(POINT_TYPES):
        species = facts.species(planting_type)
        if species:
            listed = ", ".join(f"{name} — {fmt_int(n)}" for name, n in species[:6])
            parts.append(f"{_cap(_NOM[planting_type])}: {listed}.")
    if "lawn" in intent.subjects and not parts:
        return "Газон — без пород: это площадь под травосмесь."
    return " ".join(parts) if parts else "Деревьев и кустов в плане нет — пород тоже."


def _settings_show(ctx: _Ctx) -> str:
    s = ctx.settings
    types = _join([_NOM[t] for t in ALL_TYPES if t in s.planting_types]) or "ничего"
    floor = tree_floor(s.tree_spacing_m)
    trees = f"не меньше {fmt_num(floor)} м" if floor is not None else f"по норме класса кроны ({CROWN_NORMS})"
    hedge = ctx.default_spacing.get("shrub", FALLBACK_SPACING["shrub"])
    shrubs = (
        f"{fmt_num(s.shrub_spacing_m)} м в живой изгороди"
        if s.shrub_spacing_m is not None
        else f"живая изгородь {fmt_num(hedge)} м (по умолчанию), куртины — по плотности породы"
    )
    return (
        f"Сейчас в настройках: {types}; интервал деревьев — {trees}, кустов — {shrubs}; деревья "
        f"{'двумя рядами' if s.tree_rows == 2 else 'одним рядом'}; деревья в покрытии тротуара — "
        f"{'да' if s.trees_in_paving else 'нет'}; режим {'ML' if s.scoring_mode == 'ml' else 'эвристики'}."
    )


def _summary(ctx: _Ctx) -> str:
    facts = ctx.facts
    if facts is None:
        return NO_PLAN
    texts = [_totals(facts) if facts.materialized else f"В плане {count_phrase(facts.item_count, 'planting')}."]
    texts.append(
        f"Режим {'ML' if facts.scoring_mode == 'ml' else 'эвристики'}, деревья "
        f"{'двумя рядами' if facts.tree_rows == 2 else 'одним рядом'}, в покрытии тротуара — "
        f"{'да' if facts.trees_in_paving else 'нет'}."
    )
    budgets = [b for b in facts.budgets if b.planting_type in _NOM]
    if budgets:
        first, *rest = budgets
        text = f"Под {_NOM[first.planting_type]} по нормам остаётся {_share(first.usable_share)} % участка"
        texts.append(text + "".join(f", под {_NOM[b.planting_type]} — {_share(b.usable_share)} %" for b in rest) + ".")
    if ctx.violations_count is not None:
        texts.append(_violations_status(ctx))
    return " ".join(texts)


# --- Сборка ответа ------------------------------------------------------------


class _Answer:
    def __init__(self, ctx: _Ctx):
        self.ctx = ctx
        self.pre: list[str] = []
        self.post: list[str] = []
        self.commands: list[AssistantCommand | None] = []  # None — место команды настроек
        self.settings_intents: list[_Intent] = []
        self.dropped: list[Dropped] = []
        self.greeting = False
        self.help = False
        self.thanks = False
        self.bye = False
        self.declined = False
        # Типы, у которых интервал уже менялся относительно: «слишком густо,
        # разреди» — одна просьба двумя частями, а не ×1,4 дважды.
        self.relative_done: set[str] = set()
        # Относительная смена интервала: тип -> (направление, -1 гуще / +1 реже;
        # интервал по правилам). Модельный путь сверяет с этим число модели.
        self.relative: dict[str, tuple[int, float | None]] = {}
        # «Деревья погуще» там, где гуще нормы нельзя: объяснение — после всех
        # настроек сообщения, чтобы не предлагать то, что в нём же и включили.
        self.trees_at_norm = False
        # Выделить пытались — удачно или нет (guard_commands, правило (b)).
        self.select_attempted = False
        # Операция правки -> была ли она во всех своих частях вопросом.
        self.edit_asked: dict[str, bool] = {}

    def say(self, text: str) -> None:
        if text and text not in self.pre:
            self.pre.append(text)

    def command(self, command: AssistantCommand) -> None:
        if command not in self.commands:
            self.commands.append(command)

    def settings(self, intent: _Intent) -> None:
        if not self.settings_intents:
            self.commands.append(None)
        self.settings_intents.append(intent)


def _select_intent(intent: _Intent, answer: _Answer) -> None:
    ctx = answer.ctx
    answer.select_attempted = True
    if ctx.facts is None:
        answer.say(NOTHING_TO_SELECT)
        return
    if intent.value == "all":
        target, species = "all", None
    else:
        subjects = intent.subjects
        if set(ALL_TYPES) <= set(subjects):
            target, species = "all", None
        else:
            planting_type = subjects[0]
            refusal = select_refusal(_TARGET_OF[planting_type], ctx.facts, ctx.violations_count)
            if refusal:
                answer.say(refusal)
                return
            names = intent.extra.get("species") or []
            target, species = _TARGET_OF[planting_type], (names[0] if names else None)
            if len(subjects) > 1:
                answer.say(f"За раз выделяю одну группу — {_NOM[planting_type]}.")
            if len(names) > 1:
                counts = dict(ctx.facts.species(planting_type))
                others = ", ".join(f"«{n}» ({fmt_int(counts.get(n, 0))})" for n in names[1:4])
                answer.post.append(f"Есть ещё {others} — назовите породу полностью, если нужна другая.")
    answer.command(SelectCommand(target=target, species=species))
    if intent.extra.get("confirm"):
        answer.dropped.append(Dropped(intent.extra["confirm"], "after_select"))


def _ui_intent(command: str, answer: _Answer) -> None:
    ctx = answer.ctx
    if command == "show_3d" and ctx.view == "3d":
        answer.say("План и так показан в 3D.")
    elif command == "show_2d" and ctx.view == "2d":
        answer.say("Мы и так на 2D-карте.")
    elif ctx.facts is None and command != "show_2d":
        answer.say("Плана пока нет — сначала сгенерируйте его («сгенерируй план»), потом можно скачать или посмотреть в 3D.")
    else:
        answer.command(UiCommand(command=command))
        if command == "report_json":
            answer.post.append("Для Excel есть то же таблицей — скажите «отчёт CSV».")


def _apply_settings(answer: _Answer) -> SettingsCommand | None:
    ctx = answer.ctx
    recipe = AssistantAction(**ctx.settings.model_dump())
    regenerate = False
    order = {"types_only": 0, "types_remove": 0, "types_add": 0, "types_all": 0}
    # «И так» — только про то, что уже стояло в настройках: «без нейросети,
    # обычный режим» — одна просьба двумя частями, вторая не «уже режим эвристики».
    current = ctx.settings
    for intent in sorted(answer.settings_intents, key=lambda i: order.get(i.kind, 1)):
        kind = intent.kind
        if kind == "regenerate":
            regenerate = True
        elif kind.startswith("types_"):
            _apply_types(intent, recipe, answer)
        elif kind == "rows":
            if "tree" not in recipe.planting_types:
                answer.say("Ряды — это про деревья, а они сейчас выключены: скажите «добавь деревья».")
            elif current.tree_rows == intent.value == recipe.tree_rows:
                answer.say(f"Деревья и так {'двумя рядами' if intent.value == 2 else 'одним рядом'}.")
            else:
                recipe.tree_rows = intent.value
        elif kind == "paving":
            if intent.subjects and "tree" not in intent.subjects:
                answer.say("В покрытии тротуара сажаю только деревья — с приствольной решёткой (743-ПП п. 9.8).")
            elif current.trees_in_paving == intent.value == recipe.trees_in_paving:
                answer.say(
                    "Деревья в покрытии тротуара уже разрешены." if intent.value else "Деревья в покрытии тротуара и так не сажаю."
                )
            else:
                recipe.trees_in_paving = intent.value
        elif kind == "mode":
            if current.scoring_mode == intent.value == recipe.scoring_mode:
                answer.say("Уже режим ML." if intent.value == "ml" else "Уже режим эвристики.")
            else:
                recipe.scoring_mode = intent.value
        elif kind.startswith("spacing_"):
            _apply_spacing(intent, recipe, answer)
    if answer.trees_at_norm:
        answer.say(tree_denser_text(recipe))
    before = AssistantAction(**ctx.settings.model_dump())
    if recipe == before and not regenerate:
        return None
    return SettingsCommand(settings=recipe)


def _apply_types(intent: _Intent, recipe: AssistantAction, answer: _Answer) -> None:
    current = list(recipe.planting_types)
    if intent.extra.get("keep"):
        # «Оставь деревья и газон» — то ли «только их», то ли «их не трогай».
        # Убирать типы по догадке нельзя: переспросить, а рецепт не трогать.
        if any(i.kind == "types_remove" for i in answer.settings_intents):
            return  # «оставь деревья, убери кусты» — тут просто «не трогай»
        wanted = [t for t in ALL_TYPES if t in intent.subjects]
        dropped = [_NOM[t] for t in current if t not in wanted]
        missing = [t for t in wanted if t not in current]
        names = _join([_NOM[t] for t in wanted])
        if missing:
            tail = f" (или «только {names}», если больше ничего не нужно)" if dropped else ""
            answer.say(
                f"{_cap(_join([_GEN[t] for t in missing]))} сейчас нет в настройках — скажите "
                f"«добавь {_join([_NOM[t] for t in missing])}»{tail}."
            )
        elif dropped:
            answer.say(f"Убрать {_join(dropped)} и оставить только {names}? Скажите «только {names}».")
        else:
            answer.say(f"{_cap(names)} и так в плане.")
        return
    if intent.kind == "types_all":
        new = list(ALL_TYPES)
    elif intent.kind == "types_only":
        new = [t for t in ALL_TYPES if t in intent.subjects]
    elif intent.kind == "types_remove":
        missing = [t for t in intent.subjects if t not in current]
        if missing and len(missing) == len(intent.subjects):
            answer.say(f"{_cap(_GEN[missing[0]])} и так нет в настройках.")
        new = [t for t in current if t not in intent.subjects]
    else:
        wanted = intent.subjects or list(ALL_TYPES)  # «посади всё» без перечня
        already = [t for t in wanted if t in current]
        if already and len(already) == len(wanted) and len(answer.settings_intents) == 1:
            answer.say(f"{_cap(_join([_NOM[t] for t in already]))} и так в плане.")
        new = [t for t in ALL_TYPES if t in current or t in wanted]
    if not new:
        answer.say("Совсем без посадок план не построить — какой тип оставить: деревья, кусты или газон?")
        return
    recipe.planting_types = new


def _apply_spacing(intent: _Intent, recipe: AssistantAction, answer: _Answer) -> None:
    targets = [t for t in intent.subjects if t in POINT_TYPES]
    if not targets:
        if intent.subjects:  # просили про газон
            answer.say("У газона нет интервала — он занимает всё свободное по нормам место.")
            return
        targets = [t for t in POINT_TYPES if t in recipe.planting_types]
        if not targets:
            answer.say("Интервал бывает у деревьев и кустов, а они сейчас выключены — скажите «добавь деревья».")
            return
        if intent.kind != "spacing_rel":
            answer.say("Для деревьев или кустов? Например: «интервал деревьев 6 м» или «кусты через 2,5 метра».")
            return
    for planting_type in targets:
        if intent.kind == "spacing_rel":
            if planting_type in answer.relative_done:
                continue
            answer.relative_done.add(planting_type)
        if planting_type not in recipe.planting_types:
            if intent.extra.get("enable"):
                recipe.planting_types = [t for t in ALL_TYPES if t in recipe.planting_types or t == planting_type]
                continue
            answer.say(f"{_cap(_NOM[planting_type])} сейчас выключены — скажите «добавь {_NOM[planting_type]}».")
            continue
        if planting_type == "tree":
            _apply_tree_spacing(intent, recipe, answer)
        else:
            _apply_shrub_spacing(intent, recipe, answer)


def _direction(intent: _Intent) -> int:
    """-1 — гуще, +1 — реже, 0 — интервал задан числом."""
    if intent.kind == "spacing_rel":
        return -1 if float(intent.value) < 1 else 1
    if intent.kind == "spacing_delta":
        return -1 if float(intent.value) < 0 else 1
    return 0


def _spacing_round(value: float) -> float:
    # Шаг изгороди — доли метра: 0,5 × 0,7 = 0,35, до одного знака это было бы 0,3.
    return round(value, 2) if value < 1 else round(value, 1)


def _apply_tree_spacing(intent: _Intent, recipe: AssistantAction, answer: _Answer) -> None:
    """Интервал деревьев пользователя — нижний предел поверх нормы класса
    кроны (tree_floor). «Погуще» без своего предела — ничего не меняет и
    говорит почему; со своим — уменьшает его, а дойдя до нормы узкой кроны,
    снимает совсем. «Пореже» без своего — от нормы средней кроны (5 м -> 7 м)."""
    ctx = answer.ctx
    high = SPACING_LIMITS["tree"][1]
    current = tree_floor(recipe.tree_spacing_m)
    direction = _direction(intent)
    if direction < 0 and current is None:
        answer.trees_at_norm = True
        answer.relative["tree"] = (direction, recipe.tree_spacing_m)
        return
    base = current if current is not None else ctx.default_spacing.get("tree", FALLBACK_SPACING["tree"])
    if intent.kind == "spacing_rel":
        wanted = _spacing_round(base * float(intent.value))
    elif intent.kind == "spacing_delta":
        wanted = round(base + float(intent.value), 2)
    else:
        wanted = float(intent.value)
    value = tree_floor(min(high, wanted))
    if direction:
        answer.relative["tree"] = (direction, value)
        if value == current:
            answer.say(f"Интервал деревьев уже максимальный — {fmt_num(high)} м.")
            return
    elif value is None:
        # «Интервал деревьев 2 м»: ближе нормы класса кроны дерево не встанет всё равно.
        answer.say(
            f"Чаще нормы своего класса кроны деревья не ставлю ({CROWN_NORMS}), так что {fmt_num(wanted)} м — "
            "это интервал по норме."
        )
        if current is None:
            return  # и так по норме — перестраивать нечего
    elif wanted > high:
        answer.say(f"Интервал деревьев можно до {fmt_num(high)} м — ставлю {fmt_num(high)}.")
    recipe.tree_spacing_m = value


def _apply_shrub_spacing(intent: _Intent, recipe: AssistantAction, answer: _Answer) -> None:
    """Интервал кустов — шаг живой изгороди: без своего — от шага изгороди
    по умолчанию (HEDGE_PITCH_M), а не от shrub_default 3 м, которого в плане
    нет. Нижний предел 0,3 м — граница 743-ПП табл. 3.6.2 для низкой изгороди."""
    ctx = answer.ctx
    low, high = SPACING_LIMITS["shrub"]
    current = recipe.shrub_spacing_m
    base = current if current is not None else ctx.default_spacing.get("shrub", FALLBACK_SPACING["shrub"])
    direction = _direction(intent)
    if intent.kind == "spacing_rel":
        wanted = _spacing_round(base * float(intent.value))
    elif intent.kind == "spacing_delta":
        wanted = round(base + float(intent.value), 2)
    else:
        wanted = float(intent.value)
    value = min(high, max(low, wanted))
    if direction:
        answer.relative["shrub"] = (direction, value if value != base else current)
        if value == base:
            limit = f"максимальный — {fmt_num(value)} м" if value >= high else f"минимальный — {fmt_num(value)} м: {SHRUB_MIN_NOTE}"
            answer.say(f"Интервал кустов уже {limit}.")
            return
    elif value != wanted:
        answer.say(f"Интервал кустов можно от {fmt_num(low)} до {fmt_num(high)} м — ставлю {fmt_num(value)}.")
    recipe.shrub_spacing_m = value


@dataclass
class RulesAnswer:
    """Ответ встроенного разбора — и то, что модельному пути нужно сверх
    текста и команд."""

    reply: str
    commands: list[AssistantCommand]
    # Интервалы, которые сообщение меняет относительно («кусты пореже»): тип ->
    # (направление: -1 гуще, +1 реже; интервал по правилам этого модуля).
    relative: dict[str, tuple[int, float | None]] = field(default_factory=dict)
    # Массовая правка, отложенная до второй просьбы: группа выделена этим
    # ответом, правка (delete/to_tree/to_shrub) — после подтверждения словами.
    confirms: list[str] = field(default_factory=list)
    # В сообщении пытались выделить группу (удачно или нет): правка того же
    # ответа не может относиться к текущему выделению (guard_commands, (b)).
    select_attempted: bool = False


def _compose(intents: list[_Intent], ctx: _Ctx) -> RulesAnswer:
    answer = _Answer(ctx)
    for intent in intents:
        kind = intent.kind
        if kind == "norms":
            answer.say(NORMS_REFUSAL)
        elif kind == "why_few":
            for text in _why_few(intent, ctx):
                answer.say(text)
        elif kind == "why_here":
            answer.say(_why_here(ctx))
        elif kind == "why_violations":
            answer.select_attempted = True
            if ctx.facts is not None and ctx.violations_count:
                answer.say(_why_violations(ctx))
                answer.command(SelectCommand(target="violations"))
                answer.post.append(_violations_hint(ctx))
            else:
                answer.say(_violations_status(ctx))
        elif kind == "count":
            answer.say(_count(intent, ctx))
        elif kind == "species_list":
            answer.say(_species_list(intent, ctx))
        elif kind == "summary":
            answer.say(_summary(ctx))
        elif kind == "settings_show":
            answer.say(_settings_show(ctx))
        elif kind == "violations":
            answer.select_attempted = True
            if ctx.facts is not None and ctx.violations_count:
                answer.command(SelectCommand(target="violations"))
                if intent.extra.get("confirm"):
                    answer.dropped.append(Dropped(intent.extra["confirm"], "after_select"))
                else:
                    answer.post.append(_violations_hint(ctx))
            else:
                answer.say(_violations_status(ctx))
        elif kind == "violations_status":
            answer.say(_violations_status(ctx))
        elif kind == "budget":
            for text in _budget(intent, ctx):
                answer.say(text)
        elif kind == "spatial":
            if intent.value == "select":
                answer.select_attempted = True
            answer.say(_spatial(intent))
        elif kind == "undo":
            answer.say(UNDO_TEXT)
        elif kind == "edit_declined":
            answer.declined = True
        elif kind == "deselect":
            if ctx.selected_count:
                answer.command(SelectCommand(target="none"))
            else:
                answer.say("Ничего и так не выделено.")
        elif kind == "ui":
            _ui_intent(intent.value, answer)
        elif kind == "edit":
            answer.command(EditSelectionCommand(operation=intent.value))
            # Вопрос — только если ни в одной части не просили прямо: «можно
            # удалить их? удали» — это уже просьба.
            asked = bool(intent.extra.get("asked"))
            answer.edit_asked[intent.value] = answer.edit_asked.get(intent.value, True) and asked
        elif kind == "select":
            _select_intent(intent, answer)
        elif kind == "species_absent":
            if intent.extra.get("select"):
                answer.select_attempted = True
            planting_type, name = intent.value
            answer.say(species_absent(name, planting_type, ctx.facts) if ctx.facts is not None else NO_PLAN)
        elif kind == "select_what":
            answer.select_attempted = True
            answer.say("Что выделить? Например: «выдели все деревья», «выдели липы» или «покажи нарушения».")
        elif kind == "help":
            answer.help = True
        elif kind == "greeting":
            answer.greeting = True
        elif kind == "thanks":
            answer.thanks = True
        elif kind == "bye":
            answer.bye = True
        else:
            answer.settings(intent)

    settings_command = _apply_settings(answer) if answer.settings_intents else None
    commands = [c if c is not None else settings_command for c in answer.commands]
    commands = [c for c in commands if c is not None]
    commands, dropped = guard_commands(
        commands,
        ctx.selection,
        select_attempted=answer.select_attempted,
        asked={op for op, asked in answer.edit_asked.items() if asked},
    )
    if any(isinstance(c, SelectCommand) and c.target == "violations" for c in commands):
        # «Сколько нарушений? Удали их»: «скажите «покажи нарушения»» рядом с
        # уже выделенными нарушениями — противоречие; число скажет «Выделила …».
        answer.pre = [text for text in answer.pre if text != _violations_status(ctx)]
    actions = describe_actions(
        commands,
        answer.dropped + dropped,
        current=ctx.settings,
        selection=ctx.selection,
        violations_count=ctx.violations_count,
        facts=ctx.facts,
        default_spacing=ctx.default_spacing,
    )
    parts = answer.pre + ([actions] if actions else []) + answer.post
    if answer.declined and not any(isinstance(c, EditSelectionCommand) for c in commands):
        parts = [DECLINED_TEXT] + parts
    if answer.help or (answer.greeting and not parts):
        parts = [HELP_TEXT] + parts
    elif answer.thanks and not parts:
        parts = ["Пожалуйста! Если нужно что-то ещё — пишите."]
    elif answer.bye and not parts:
        parts = ["Спасибо! Если понадобится помощь с планом — пишите."]
    if not parts:
        return RulesAnswer(FALLBACK_TEXT, [])
    reply = " ".join(dict.fromkeys(parts))
    if answer.greeting:
        reply = "Привет! " + reply
    confirms = list(dict.fromkeys(d.operation for d in answer.dropped + dropped if d.reason == "after_select"))
    return RulesAnswer(reply, commands, dict(answer.relative), confirms, answer.select_attempted)


def rules_answer(
    message: str,
    settings: AssistantGenerationSettings,
    facts: PlanFacts | None = None,
    selection: AssistantSelection | None = None,
    violations_count: int | None = None,
    view: str = "2d",
    default_spacing: dict[str, float] | None = None,
) -> RulesAnswer:
    """Ответ и команды по тексту — без языковой модели. Никогда не бросает на
    странном вводе: худший случай — «не поняла» со списком того, что умеет."""
    ctx = _Ctx(
        settings=settings,
        facts=facts,
        selection=selection,
        violations_count=violations_count,
        view=view,
        default_spacing={**FALLBACK_SPACING, **(default_spacing or {})},
    )
    if not split_clauses(message) and "?" in (message or ""):
        return RulesAnswer(HELP_TEXT, [])  # одинокий «?» — это «что ты умеешь»
    return _compose(_parse(message, ctx), ctx)


def answer_with_rules(
    message: str,
    settings: AssistantGenerationSettings,
    facts: PlanFacts | None = None,
    selection: AssistantSelection | None = None,
    violations_count: int | None = None,
    view: str = "2d",
    default_spacing: dict[str, float] | None = None,
) -> tuple[str, list[AssistantCommand]]:
    """rules_answer без подробностей для модельного пути: (ответ, команды)."""
    answer = rules_answer(message, settings, facts, selection, violations_count, view, default_spacing)
    return answer.reply, answer.commands
