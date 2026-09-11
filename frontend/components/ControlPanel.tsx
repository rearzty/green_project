"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { EditMode, PlanSummary, PlantingType, ScoringMode, ValidationViolation } from "@/lib/api";

const PLANTING_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Деревья",
  shrub: "Кустарники",
  lawn: "Газон",
};

const EDIT_MODE_LABELS: Record<Exclude<EditMode, "none">, string> = {
  remove_within_radius: "Убрать посадки в радиусе",
  exclude_polygon: "Исключить область (полигон)",
  replace_type_in_zone: "Заменить тип в области",
};

function formatPlanLabel(plan: PlanSummary): string {
  const mode = plan.scoring_mode === "ml" ? "ML" : "Эвристика";
  const time = new Date(plan.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  return `${mode} · ${time} · ${plan.item_count} шт.${plan.is_current ? " · текущий" : ""}`;
}

export interface ControlPanelProps {
  hasProject: boolean;
  hasPlan: boolean;
  /** True only during the brief client-side restore-from-localStorage pass
   * right after the page loads (see app/page.tsx) — nothing to click yet,
   * just avoids a flash of the empty "no project" state while that fetch
   * is in flight. */
  restoring?: boolean;
  /** null = validation hasn't been run yet for the current plan. */
  violations: ValidationViolation[] | null;
  onUpload: (file: File, name: string, sourceCrs: string) => Promise<void>;
  onGenerate: (plantingTypes: PlantingType[], scoringMode: ScoringMode) => Promise<void>;
  onValidate: () => Promise<void>;
  /** Forgets the locally-remembered project/plan (see app/page.tsx) and
   * resets the UI to its empty state. Local-only — does not delete anything
   * from the backend. */
  onClearAll: () => void;
  exportHref?: string;
  /** Whether the imported utility/zone layers and the generated plan are
   * currently drawn on the map — separate from *having* them (hasProject/
   * hasPlan), so a project can stay loaded but be hidden to declutter the
   * view when several uploads' worth of geometry would otherwise stack. */
  showLayers: boolean;
  showPlan: boolean;
  onToggleShowLayers: (show: boolean) => void;
  onToggleShowPlan: (show: boolean) => void;
  /** Every plan generated so far for the current project — each `generate`
   * call makes a new one rather than overwriting the last, which used to
   * look like heuristic/ml results "getting mixed up" with no way to tell
   * which plan was actually on screen. */
  plans: PlanSummary[];
  currentPlanId?: string;
  onSelectPlan: (planId: string) => Promise<void>;
  editMode: EditMode;
  onSetEditMode: (mode: EditMode) => void;
  editRadiusM: number;
  onSetEditRadiusM: (radius: number) => void;
  editTargetType: PlantingType;
  onSetEditTargetType: (type: PlantingType) => void;
  canApplyEdit: boolean;
  onApplyEdit: () => Promise<void>;
  onCancelEdit: () => void;
}

export function ControlPanel({
  hasProject,
  hasPlan,
  restoring = false,
  violations,
  onUpload,
  onGenerate,
  onValidate,
  onClearAll,
  exportHref,
  showLayers,
  showPlan,
  onToggleShowLayers,
  onToggleShowPlan,
  plans,
  currentPlanId,
  onSelectPlan,
  editMode,
  onSetEditMode,
  editRadiusM,
  onSetEditRadiusM,
  editTargetType,
  onSetEditTargetType,
  canApplyEdit,
  onApplyEdit,
  onCancelEdit,
}: ControlPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("Тестовая территория");
  // Defaults to the CRS scripts/generate_synthetic_data.py writes test data
  // in (UTM 37N, covers Moscow) so synthetic uploads land on the real map
  // out of the box — clear/replace it once real Mosgeotrest data (unknown
  // CRS until 15.09) is being uploaded instead.
  const [sourceCrs, setSourceCrs] = useState("EPSG:32637");
  const [plantingTypes, setPlantingTypes] = useState<PlantingType[]>(["tree", "shrub", "lawn"]);
  const [scoringMode, setScoringMode] = useState<ScoringMode>("heuristic");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleType(type: PlantingType) {
    setPlantingTypes((prev) => (prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]));
  }

  return (
    <aside className="flex h-full w-80 flex-col gap-4 overflow-y-auto border-r border-stone-200 bg-white p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-greenery-700">GreenProject</h1>
          <p className="text-xs text-stone-500">Автопроектирование озеленения территории</p>
        </div>
        {hasProject && (
          <button
            onClick={onClearAll}
            title="Забыть текущий проект в этом браузере (в базе данных ничего не удаляется)"
            className="whitespace-nowrap rounded border border-stone-300 px-2 py-1 text-xs text-stone-600 hover:bg-stone-50"
          >
            Очистить всё
          </button>
        )}
      </div>
      {restoring && <p className="text-xs text-stone-500">Восстановление сессии…</p>}

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium">1. Загрузить территорию</h2>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Название проекта"
          className="rounded border border-stone-300 px-2 py-1 text-sm"
        />
        <input
          type="text"
          value={sourceCrs}
          onChange={(e) => setSourceCrs(e.target.value)}
          placeholder="CRS (необязательно, напр. EPSG:32637)"
          className="rounded border border-stone-300 px-2 py-1 text-sm"
        />
        <input
          type="file"
          accept=".dxf,.geojson,.json,.shp"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-sm"
        />
        <Button
          disabled={!file || busy}
          onClick={() => file && guarded(() => onUpload(file, name, sourceCrs))}
        >
          Загрузить
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">2. Сгенерировать план</h2>
        <div className="flex flex-col gap-1 text-sm">
          {(Object.keys(PLANTING_TYPE_LABELS) as PlantingType[]).map((type) => (
            <label key={type} className="flex items-center gap-2">
              <input type="checkbox" checked={plantingTypes.includes(type)} onChange={() => toggleType(type)} />
              {PLANTING_TYPE_LABELS[type]}
            </label>
          ))}
        </div>
        <div className="flex gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input
              type="radio"
              checked={scoringMode === "heuristic"}
              onChange={() => setScoringMode("heuristic")}
            />
            Эвристика
          </label>
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "ml"} onChange={() => setScoringMode("ml")} />
            ML
          </label>
        </div>
        <Button
          disabled={!hasProject || plantingTypes.length === 0 || busy}
          onClick={() => guarded(() => onGenerate(plantingTypes, scoringMode))}
        >
          Сгенерировать план
        </Button>
      </section>

      {plans.length > 0 && (
        <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
          <h2 className="text-sm font-medium">История планов</h2>
          <ul className="flex max-h-32 flex-col gap-1 overflow-y-auto text-xs">
            {plans.map((p) => (
              <li key={p.plan_id}>
                <button
                  disabled={busy}
                  onClick={() => guarded(() => onSelectPlan(p.plan_id))}
                  className={`w-full rounded border px-2 py-1 text-left ${
                    p.plan_id === currentPlanId ? "border-greenery-500 bg-greenery-50" : "border-stone-200"
                  }`}
                >
                  {formatPlanLabel(p)}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">Ручная правка плана</h2>
        <p className="text-xs text-stone-500">
          Клик по дереву/кусту на карте — сменить тип или удалить; перетаскивание — переместить. Операции ниже — массовые, по области.
        </p>
        <select
          value={editMode}
          disabled={!hasPlan}
          onChange={(e) => onSetEditMode(e.target.value as EditMode)}
          className="rounded border border-stone-300 px-2 py-1 text-sm"
        >
          <option value="none">Не редактировать</option>
          {(Object.keys(EDIT_MODE_LABELS) as (keyof typeof EDIT_MODE_LABELS)[]).map((mode) => (
            <option key={mode} value={mode}>
              {EDIT_MODE_LABELS[mode]}
            </option>
          ))}
        </select>
        {editMode === "remove_within_radius" && (
          <label className="flex items-center gap-2 text-sm">
            Радиус, м
            <input
              type="number"
              min={1}
              value={editRadiusM}
              onChange={(e) => onSetEditRadiusM(Number(e.target.value))}
              className="w-20 rounded border border-stone-300 px-2 py-1"
            />
          </label>
        )}
        {editMode === "replace_type_in_zone" && (
          <label className="flex items-center gap-2 text-sm">
            Новый тип
            <select value={editTargetType} onChange={(e) => onSetEditTargetType(e.target.value as PlantingType)} className="rounded border border-stone-300 px-2 py-1">
              {(Object.keys(PLANTING_TYPE_LABELS) as PlantingType[]).map((type) => (
                <option key={type} value={type}>
                  {PLANTING_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
          </label>
        )}
        {editMode !== "none" && (
          <p className="text-xs text-stone-500">
            {editMode === "remove_within_radius" ? "Кликните на карте, чтобы поставить центр." : "Кликайте на карте, чтобы отметить вершины области."}
          </p>
        )}
        {editMode !== "none" && (
          <div className="flex gap-2">
            <Button disabled={!canApplyEdit || busy} onClick={() => guarded(onApplyEdit)} className="flex-1">
              Применить
            </Button>
            <Button variant="outline" onClick={onCancelEdit} className="flex-1">
              Отмена
            </Button>
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">Отображение на карте</h2>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showLayers} onChange={(e) => onToggleShowLayers(e.target.checked)} />
          Исходные слои (сети, здания, зонирование)
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showPlan} onChange={(e) => onToggleShowPlan(e.target.checked)} />
          Сгенерированный план
        </label>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">3. Проверить и экспортировать</h2>
        <Button variant="outline" disabled={!hasPlan || busy} onClick={() => guarded(onValidate)}>
          Проверить нормативы
        </Button>
        {violations !== null && violations.length > 0 && (
          <ul className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
            {violations.map((v) => (
              <li key={v.item_id}>{v.message}</li>
            ))}
          </ul>
        )}
        {violations !== null && violations.length === 0 && (
          <p className="text-xs text-greenery-700">Нарушений отступов не найдено.</p>
        )}
        <a href={exportHref} aria-disabled={!hasPlan}>
          <Button variant="outline" disabled={!hasPlan} className="w-full">
            Экспорт в DXF
          </Button>
        </a>
      </section>

      {error && <p className="rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    </aside>
  );
}
