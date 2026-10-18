/** Юна: всё, что про её команды можно посчитать без React, — тексты итогов,
 * отбор посадок для «выдели …», подсказки по текущему состоянию. Само
 * исполнение команд — в app/page.tsx: оно идёт теми же путями, что кнопки
 * панели и карточки, и им нужны хуки страницы. */

import type {
  AssistantAction,
  AssistantCommand,
  AssistantGenerationSettings,
  AssistantSelection,
  GeoJSONFeatureCollection,
  PlantingType,
  ScoringMode,
} from "@/lib/api";
import { pluralRu } from "@/lib/format";
import type { PlanIndex } from "@/lib/planIndex";

/** Итог одной команды — строка под ответом Юны. */
export interface CommandResult {
  ok: boolean;
  text: string;
  /** Отменить именно эту команду. Отказ (после неё план уже менялся, сеть)
   * — исключение с готовым текстом для пользователя. */
  undo?: () => void | Promise<void>;
}

export type SelectTarget = Extract<AssistantCommand, { type: "select" }>["target"];
export type UiAction = Extract<AssistantCommand, { type: "ui" }>["command"];

type Forms = [one: string, few: string, many: string];

/** Существительное итогов: формы при числе и род — от рода зависит
 * причастие при единице («удалена посадка», «удалён куст», «удалено дерево»). */
export interface Noun {
  forms: Forms;
  gender: "m" | "f" | "n";
}

// В чате говорим «кусты», как сам пользователь («кусты пореже»), а не
// «кустарник», как подписи панели.
export const PLANTING: Noun = { forms: ["посадка", "посадки", "посадок"], gender: "f" };
/** «Удаляю 1 посадку» — винительный падеж. */
export const PLANTING_ACC: Forms = ["посадку", "посадки", "посадок"];
/** «Меняю тип у 21 посадки» — родительный. */
export const PLANTING_GEN: Forms = ["посадки", "посадок", "посадок"];
const TREE: Noun = { forms: ["дерево", "дерева", "деревьев"], gender: "n" };
const SHRUB: Noun = { forms: ["куст", "куста", "кустов"], gender: "m" };
const LAWN: Noun = { forms: ["участок газона", "участка газона", "участков газона"], gender: "m" };

/** «1 234 дерева» — с разделителем разрядов, в отличие от format.ts::countLabel:
 * в итогах Юны бывают десятки тысяч кустов. */
export function countText(n: number, forms: Forms): string {
  return `${n.toLocaleString("ru-RU")} ${pluralRu(n, forms)}`;
}

const ONE_ENDING: Record<Noun["gender"], string> = { m: "", f: "а", n: "о" };

/** «Удалена 1 посадка», «Удалены 3 посадки», «Удалено 5 посадок», «Удалена
 * 21 посадка»: краткое причастие согласуется с числом по тем же правилам,
 * что и существительное (pluralRu), а при единице — ещё и с родом. `stem` —
 * основа без окончания: «Удален», «Выделен». */
export function doneText(stem: string, n: number, noun: Noun): string {
  return `${pluralRu(n, [stem + ONE_ENDING[noun.gender], `${stem}ы`, `${stem}о`])} ${countText(n, noun.forms)}`;
}

// --- настройки генерации ---

const TYPE_ORDER: PlantingType[] = ["tree", "shrub", "lawn"];
const TYPE_NAMES: Record<PlantingType, string> = { tree: "деревья", shrub: "кусты", lawn: "газон" };
const MODE_NAMES: Record<ScoringMode, string> = { heuristic: "эвристика", ml: "ML-модель" };

function typesText(types: PlantingType[]): string {
  return TYPE_ORDER.filter((t) => types.includes(t)).map((t) => TYPE_NAMES[t]).join(", ") || "ничего";
}

function spacingText(m: number | null | undefined): string {
  return m == null ? "по норме" : `${m.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} м`;
}

function sameSpacing(a: number | null | undefined, b: number | null | undefined): boolean {
  if (a == null || b == null) return a == null && b == null;
  return Math.abs(a - b) < 1e-6;
}

/** Рецепт из команды Юны — полный (contract: SettingsCommand), но JSON несёт
 * null там, где интервал «по норме», а панель хранит undefined. Недостающее
 * поле (старый бэкенд) — как сейчас на панели. */
export function settingsFromAction(action: AssistantAction, current: AssistantGenerationSettings): AssistantGenerationSettings {
  return {
    planting_types: action.planting_types ?? current.planting_types,
    tree_spacing_m: action.tree_spacing_m ?? undefined,
    shrub_spacing_m: action.shrub_spacing_m ?? undefined,
    scoring_mode: action.scoring_mode ?? current.scoring_mode,
    tree_rows: action.tree_rows ?? current.tree_rows,
    trees_in_paving: action.trees_in_paving ?? current.trees_in_paving,
  };
}

/** Что поменялось — «интервал кустов: по норме → 4,2 м», по пункту на поле. */
export function describeSettingsChange(prev: AssistantGenerationSettings, next: AssistantGenerationSettings): string[] {
  const changes: string[] = [];
  const prevTypes = typesText(prev.planting_types);
  const nextTypes = typesText(next.planting_types);
  if (prevTypes !== nextTypes) changes.push(`посадки: ${prevTypes} → ${nextTypes}`);
  if (!sameSpacing(prev.tree_spacing_m, next.tree_spacing_m)) {
    changes.push(`интервал деревьев: ${spacingText(prev.tree_spacing_m)} → ${spacingText(next.tree_spacing_m)}`);
  }
  if (!sameSpacing(prev.shrub_spacing_m, next.shrub_spacing_m)) {
    changes.push(`интервал кустов: ${spacingText(prev.shrub_spacing_m)} → ${spacingText(next.shrub_spacing_m)}`);
  }
  if (prev.tree_rows !== next.tree_rows) changes.push(`двухрядная аллея: ${next.tree_rows >= 2 ? "вкл" : "выкл"}`);
  if (prev.trees_in_paving !== next.trees_in_paving) changes.push(`деревья в тротуаре: ${next.trees_in_paving ? "вкл" : "выкл"}`);
  if (prev.scoring_mode !== next.scoring_mode) {
    changes.push(`выбор мест: ${MODE_NAMES[prev.scoring_mode]} → ${MODE_NAMES[next.scoring_mode]}`);
  }
  return changes;
}

/** «118 деревьев, 960 кустов, газон 1 240 м²» — по сводке плана (planStats.ts). */
export function planCountsText(stats: { trees: number; shrubs: number; lawnAreaM2: number }): string {
  const parts: string[] = [];
  if (stats.trees > 0) parts.push(countText(stats.trees, TREE.forms));
  if (stats.shrubs > 0) parts.push(countText(stats.shrubs, SHRUB.forms));
  if (stats.lawnAreaM2 >= 1) parts.push(`газон ${Math.round(stats.lawnAreaM2).toLocaleString("ru-RU")} м²`);
  return parts.join(", ") || "посадок нет";
}

// --- выделение ---

type GroupTarget = Exclude<SelectTarget, "violations" | "none">;

const TARGET_TYPES: Record<Exclude<GroupTarget, "all">, PlantingType> = { trees: "tree", shrubs: "shrub", lawn: "lawn" };
const TARGET_NOUNS: Record<GroupTarget, Noun> = { trees: TREE, shrubs: SHRUB, lawn: LAWN, all: PLANTING };
const TARGET_MISSING: Record<GroupTarget, string> = {
  trees: "Деревьев в плане нет.",
  shrubs: "Кустов в плане нет.",
  lawn: "Газона в плане нет.",
  all: "В плане нет посадок.",
};

function normalizeName(s: string): string {
  return s.toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim();
}

/** Id посадок группы. Порода — подстрокой без регистра и «ё»: Юна может
 * сказать «липа», а в плане «Липа мелколистная». */
export function idsForTarget(index: PlanIndex, features: GeoJSONFeatureCollection, target: GroupTarget, species?: string | null): string[] {
  const type = target === "all" ? undefined : TARGET_TYPES[target];
  const query = species ? normalizeName(species) : "";
  if (!query) {
    const ids: string[] = [];
    index.forEach((item) => {
      if (!type || item.type === type) ids.push(item.id);
    });
    return ids;
  }
  // Породы в индексе нет — она только в свойствах фич, которые
  // useProjectSession держит в синхроне с индексом на каждой правке.
  const ids: string[] = [];
  for (const feature of features.features) {
    const props = feature.properties as { id?: unknown; planting_type?: string; species?: string };
    if (type && props.planting_type !== type) continue;
    if (!props.species || !normalizeName(props.species).includes(query)) continue;
    const id = String(props.id);
    if (index.has(id)) ids.push(id);
  }
  return ids;
}

/** «Выделены 3 дерева — «липа»» — строка итога целиком. */
export function selectedText(target: GroupTarget, n: number, species?: string | null): string {
  const base = doneText("Выделен", n, TARGET_NOUNS[target]);
  return species ? `${base} — «${species}»` : base;
}

/** «Выделена 1 посадка с нарушениями — она отмечена красным». */
export function violationsSelectedText(n: number): string {
  return `${doneText("Выделен", n, PLANTING)} с нарушениями — ${n === 1 ? "она отмечена" : "они отмечены"} красным`;
}

export function missingText(target: GroupTarget, species?: string | null): string {
  return species ? `Посадок «${species}» в плане нет.` : TARGET_MISSING[target];
}

// --- подсказки ---

export interface SuggestionState {
  hasProject: boolean;
  hasPlan: boolean;
  settings: AssistantGenerationSettings;
  /** Сколько деревьев в плане на экране (planStats). */
  planTrees: number;
  /** null — план ещё не проверен на нормативы. */
  violationsCount: number | null;
  /** null/undefined или count 0 — ничего не выделено. */
  selection?: AssistantSelection | null;
}

const MAX_SUGGESTIONS = 5;

/** Готовые фразы по тому, что сейчас на экране. Только те, что понимает и
 * базовый режим без ИИ (backend/app/services/assistant_rules.py), —
 * подсказка, на которую Юна без ключа ответила бы «не поняла», хуже, чем
 * никакой. */
export function suggestionChips(state: SuggestionState): string[] {
  if (!state.hasProject) return [];
  if (!state.hasPlan) return ["Сгенерируй план", "Что ты умеешь?"];
  const chips: string[] = [];
  const selection = state.selection;
  if (selection && selection.count === 1) {
    chips.push("Почему эта посадка здесь?");
    // Газон — площадной объект, его тип не меняется (usePlanEdits.ts).
    if (selection.trees === 1) chips.push("Сделай кустом");
    else if (selection.shrubs === 1) chips.push("Сделай деревом");
    chips.push("Удали выделенную");
  } else if (selection && selection.count > 1) {
    chips.push("Удали выделенные");
    if (selection.shrubs > 0) chips.push("Сделай их деревьями");
    if (selection.trees > 0) chips.push("Сделай их кустами");
  }
  // Нарушения — первыми из общих: это то, что стоит исправить.
  if (state.violationsCount) chips.push("Покажи нарушения");
  chips.push("Почему так мало деревьев?");
  if (state.settings.planting_types.includes("shrub")) chips.push("Кусты пореже");
  if (state.settings.planting_types.includes("tree") && state.settings.tree_rows < 2) chips.push("Посади деревья двумя рядами");
  if (state.planTrees > 0) chips.push("Выдели все деревья");
  return chips.slice(0, MAX_SUGGESTIONS);
}
