/** Схемы посадки «в один клик» (Generate.dc.html, п. 8): наборы уже
 * существующих параметров генерации, а не новый режим бэкенда. Выбранная схема
 * не хранится — она ВЫВОДИТСЯ из текущих значений полей (`matchPreset`): тронул
 * поле в «Тонкой настройке» — значения перестали совпадать ни с одной схемой, и
 * выбор снимается сам, без отдельного состояния, которое можно забыть
 * сбросить (так же устроен рецепт плана после перезагрузки или смены версии). */

export type PresetKey = "norms" | "dense" | "alley";

export interface PresetSettings {
  treeSpacingM: number | undefined;
  shrubSpacingM: number | undefined;
  treeRows: number;
  treesInPaving: boolean;
}

export interface PlanPreset {
  key: PresetKey;
  title: string;
  note: string;
  settings: PresetSettings;
}

// Интервалы у всех схем — по нормам (undefined): задать «меньше нормы» нельзя,
// пользовательское значение только раздвигает деревья (geo_engine/planner.py),
// так что «минимальные интервалы» — это и есть нормативные.
export const PLAN_PRESETS: PlanPreset[] = [
  {
    key: "norms",
    title: "По нормам",
    note: "Интервал по кроне, один ряд",
    settings: { treeSpacingM: undefined, shrubSpacingM: undefined, treeRows: 1, treesInPaving: false },
  },
  {
    key: "dense",
    title: "Плотнее",
    note: "Минимальные интервалы и деревья в покрытии тротуара (решётка Ø 2 м)",
    settings: { treeSpacingM: undefined, shrubSpacingM: undefined, treeRows: 1, treesInPaving: true },
  },
  {
    key: "alley",
    title: "Аллея",
    note: "Два ряда в шахматном порядке, 7–8 м",
    settings: { treeSpacingM: undefined, shrubSpacingM: undefined, treeRows: 2, treesInPaving: false },
  },
];

/** Какая схема совпадает с текущими значениями; null — настройки свои. */
export function matchPreset(current: PresetSettings): PresetKey | null {
  const hit = PLAN_PRESETS.find(
    (p) =>
      p.settings.treeSpacingM === current.treeSpacingM &&
      p.settings.shrubSpacingM === current.shrubSpacingM &&
      p.settings.treeRows === current.treeRows &&
      p.settings.treesInPaving === current.treesInPaving
  );
  return hit?.key ?? null;
}
