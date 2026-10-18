/** Рецепт генерации, с которым построен план, — чтобы поля панели (и то, что
 * уходит Юне как текущие настройки) показывали план на экране, а не
 * значения по умолчанию после обновления страницы или переключения версии.
 * Иначе двухрядный план, восстановленный после обновления, показывался с
 * выключенной «Двухрядной аллеей», и следующая просьба к Юне («кусты
 * пореже») молча строила однорядный.
 *
 * Источник правды — сам PlanOut, если бэкенд прислал рецепт целиком. Пока не
 * присылает — то, что этот браузер запомнил, когда сам генерировал план
 * (useProjectSession.handleGenerate): фронт знает отправленный рецепт, а
 * _find_reusable_plan возвращает готовый план только при совпадении рецепта
 * целиком, так что запись не врёт. */

import type { AssistantGenerationSettings, PlanOut, PlantingType, ScoringMode } from "@/lib/api";

const STORAGE_KEY = "greenproject:plan-recipes";
/** id планов уникальны, записи не протухают — предел только против роста. */
const MAX_ENTRIES = 200;

const PLANTING_TYPES: PlantingType[] = ["tree", "shrub", "lawn"];

type StoredRecipes = Record<string, AssistantGenerationSettings>;

function load(): StoredRecipes {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === "object" ? (parsed as StoredRecipes) : {};
  } catch {
    return {}; // хранилище выключено или запись испорчена — просто нечего вспомнить
  }
}

export function rememberPlanRecipe(planId: string, recipe: AssistantGenerationSettings) {
  try {
    const recipes = load();
    delete recipes[planId]; // свежая запись — в конец, вытесняются старейшие
    recipes[planId] = recipe;
    const ids = Object.keys(recipes);
    for (const id of ids.slice(0, Math.max(0, ids.length - MAX_ENTRIES))) delete recipes[id];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(recipes));
  } catch {
    // без памяти рецептов панель просто не восстановит поля — не повод для ошибки
  }
}

function validTypes(types: unknown): PlantingType[] | null {
  if (!Array.isArray(types)) return null;
  const known = PLANTING_TYPES.filter((t) => types.includes(t));
  return known.length > 0 ? known : null;
}

function spacing(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

/** Рецепт плана на экране. Чего не знает ни бэкенд, ни память браузера (план
 * построен в другой вкладке или до этой правки), остаётся как в `current`;
 * режим отбора план знает всегда, типы посадок — по разбору площади
 * (area_budget считается ровно по запрошенным типам). */
export function recipeForPlan(plan: PlanOut, current: AssistantGenerationSettings): AssistantGenerationSettings {
  const scoringMode: ScoringMode = plan.scoring_mode;
  const fromServer = validTypes(plan.planting_types);
  if (fromServer && typeof plan.tree_rows === "number" && typeof plan.trees_in_paving === "boolean") {
    return {
      planting_types: fromServer,
      tree_spacing_m: spacing(plan.tree_spacing_m),
      shrub_spacing_m: spacing(plan.shrub_spacing_m),
      scoring_mode: scoringMode,
      tree_rows: plan.tree_rows,
      trees_in_paving: plan.trees_in_paving,
    };
  }
  const stored = load()[plan.plan_id];
  const storedTypes = validTypes(stored?.planting_types);
  if (stored && storedTypes) {
    return {
      planting_types: storedTypes,
      tree_spacing_m: spacing(stored.tree_spacing_m),
      shrub_spacing_m: spacing(stored.shrub_spacing_m),
      scoring_mode: scoringMode,
      tree_rows: stored.tree_rows === 2 ? 2 : 1,
      trees_in_paving: stored.trees_in_paving === true,
    };
  }
  const budgetTypes = validTypes((plan.area_budget ?? []).map((b) => b.planting_type));
  return { ...current, scoring_mode: scoringMode, planting_types: budgetTypes ?? current.planting_types };
}
