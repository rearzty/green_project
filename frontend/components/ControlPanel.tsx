"use client";

import { useMemo, useState } from "react";
import { Loader2, Redo2, Shrub, SquareDashedMousePointer, Trash2, TreeDeciduous, Undo2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import type { PlanSummary, PlantingType, ScoringMode, ValidationViolation } from "@/lib/api";
import { countLabel, OBJECT_FORMS, PLANTING_TYPE_FORMS, PLANTING_TYPE_LABELS } from "@/lib/format";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import type { LayerLegendEntry } from "@/lib/mapStyle";
import { errorMessage, toast } from "@/lib/toast";

export type { SelectionSummary };

const GENERATE_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Деревья",
  shrub: "Кустарники",
  lawn: "Газон",
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
   * right after the page loads (see app/page.tsx). */
  restoring?: boolean;
  /** True while a generate job is running server-side (candidate generation
   * + placement can take tens of seconds on a real-scale territory) --
   * generation now runs as a background job the frontend polls, see
   * lib/api.ts::generatePlan. */
  generating?: boolean;
  /** True while a DXF export job is running server-side -- writing a
   * real-scale plan can take minutes, so this also runs as a background job
   * the frontend polls, see lib/api.ts::exportDxf. */
  exporting?: boolean;
  onUpload: (file: File, name: string, sourceCrs: string) => Promise<void>;
  onGenerate: (plantingTypes: PlantingType[], scoringMode: ScoringMode) => Promise<void>;
  onExportDxf: () => Promise<void>;
  /** Forgets the locally-remembered project/plan (see app/page.tsx) and
   * resets the UI to its empty state. Local-only — does not delete anything
   * from the backend. */
  onClearAll: () => void;
  /** What's actually in the loaded layers right now (see
   * lib/mapStyle.ts::buildLayerLegend) -- one row per type actually
   * present, not a fixed list, so a project with no roads/utilities
   * doesn't show empty toggles for them. */
  layerLegend: LayerLegendEntry[];
  hiddenLayerTypes: ReadonlySet<string>;
  onToggleLayerType: (key: string) => void;
  showPlan: boolean;
  onToggleShowPlan: (show: boolean) => void;
  /** Every plan generated so far for the current project. */
  plans: PlanSummary[];
  currentPlanId?: string;
  onSelectPlan: (planId: string) => Promise<void>;

  /** An edit is being saved — edit actions and plan switching wait for it. */
  editBusy: boolean;
  /** Last-5-edits local undo/redo — see lib/undoStack.ts. */
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  selectMode: boolean;
  onToggleSelectMode: () => void;
  selection: SelectionSummary | null;
  onRetypeSelection: (type: "tree" | "shrub") => void;
  onDeleteSelection: () => void;
  onClearSelection: () => void;
  onSelectAll: () => void;

  /** Re-checked automatically after every edit and whenever a plan opens. */
  validating: boolean;
  /** null = not checked yet for the current plan. */
  violations: ValidationViolation[] | null;
  onFocusItem: (itemId: string) => void;
}

function describeSelection(selection: SelectionSummary): string {
  if (selection.single) {
    const score = Number.isFinite(selection.single.score) ? ` · оценка ${selection.single.score.toFixed(2)}` : "";
    return `${PLANTING_TYPE_LABELS[selection.single.type]}${score}`;
  }
  return (Object.keys(selection.counts) as PlantingType[])
    .filter((type) => selection.counts[type] > 0)
    .map((type) => countLabel(selection.counts[type], PLANTING_TYPE_FORMS[type]))
    .join(", ");
}

export function ControlPanel({
  hasProject,
  hasPlan,
  restoring = false,
  generating = false,
  exporting = false,
  onUpload,
  onGenerate,
  onExportDxf,
  onClearAll,
  layerLegend,
  hiddenLayerTypes,
  onToggleLayerType,
  showPlan,
  onToggleShowPlan,
  plans,
  currentPlanId,
  onSelectPlan,
  editBusy,
  canUndo,
  canRedo,
  onUndo,
  onRedo,
  selectMode,
  onToggleSelectMode,
  selection,
  onRetypeSelection,
  onDeleteSelection,
  onClearSelection,
  onSelectAll,
  validating,
  violations,
  onFocusItem,
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
  // Per violation group: how many times "show on map" was clicked, to step through its items.
  const [violationCursor, setViolationCursor] = useState<Record<string, number>>({});

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleType(type: PlantingType) {
    setPlantingTypes((prev) => (prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]));
  }

  // Every violation of one kind carries the same message -- one row per
  // kind with a count reads better than the same sentence repeated N times.
  const violationGroups = useMemo(() => {
    const groups = new Map<string, string[]>();
    for (const v of violations ?? []) {
      const ids = groups.get(v.message) ?? [];
      ids.push(v.item_id);
      groups.set(v.message, ids);
    }
    return [...groups.entries()];
  }, [violations]);

  const hasSelection = selection !== null && selection.total > 0;
  const canEdit = hasPlan && !editBusy;

  return (
    <aside className="flex h-full w-80 flex-none flex-col gap-4 overflow-y-auto border-r border-stone-200 bg-white p-4">
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
        <input type="file" accept=".dxf,.geojson,.json,.shp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-sm" />
        <Button disabled={!file || busy} onClick={() => file && guarded(() => onUpload(file, name, sourceCrs))}>
          Загрузить
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">2. Сгенерировать план</h2>
          {generating && (
            <span className="flex items-center gap-1 text-xs text-stone-500">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Генерация…
            </span>
          )}
        </div>
        <div className="flex flex-col gap-1 text-sm">
          {(Object.keys(GENERATE_TYPE_LABELS) as PlantingType[]).map((type) => (
            <label key={type} className="flex items-center gap-2">
              <input type="checkbox" checked={plantingTypes.includes(type)} onChange={() => toggleType(type)} />
              {GENERATE_TYPE_LABELS[type]}
            </label>
          ))}
        </div>
        <div className="flex gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "heuristic"} onChange={() => setScoringMode("heuristic")} />
            Эвристика
          </label>
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "ml"} onChange={() => setScoringMode("ml")} />
            ML
          </label>
        </div>
        <Button
          disabled={!hasProject || plantingTypes.length === 0 || busy || editBusy || generating}
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
                  disabled={busy || editBusy}
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
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">Правка плана</h2>
          {editBusy && (
            <span className="flex items-center gap-1 text-xs text-stone-500">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Сохранение…
            </span>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <Button variant="outline" size="sm" disabled={!canUndo || !canEdit} onClick={onUndo} title="Отменить последнюю правку">
            <Undo2 className="mr-1.5 h-4 w-4" aria-hidden />
            Отменить
            <Kbd className="ml-auto">Ctrl+Z</Kbd>
          </Button>
          <Button variant="outline" size="sm" disabled={!canRedo || !canEdit} onClick={onRedo} title="Повторить отменённую правку">
            <Redo2 className="mr-1.5 h-4 w-4" aria-hidden />
            Повторить
            <Kbd className="ml-auto">Ctrl+Y</Kbd>
          </Button>
        </div>

        <Button
          variant={selectMode ? "default" : "outline"}
          size="sm"
          disabled={!selectMode && (!hasPlan || !showPlan)}
          onClick={onToggleSelectMode}
          aria-pressed={selectMode}
          title="Левой кнопкой по карте — рамка выделения вместо перемещения карты"
        >
          <SquareDashedMousePointer className="mr-1.5 h-4 w-4" aria-hidden />
          {selectMode ? "Режим выделения: вкл" : "Режим выделения: выкл"}
          <Kbd className={`ml-auto ${selectMode ? "border-greenery-500 bg-greenery-700 text-white" : ""}`}>S</Kbd>
        </Button>

        <p className="text-xs text-stone-500">
          {selectMode
            ? "Потяните по карте — выделить рамкой (с Shift — добавить к выделению). Потяните выделенное — переместить."
            : "Клик по объекту — выделить (Shift — несколько), перетаскивание точки — переместить. Для рамки включите режим выделения."}{" "}
          ПКМ — меню действий.
        </p>

        <div className={`flex flex-col gap-2 rounded-md border p-2 ${hasSelection ? "border-blue-200 bg-blue-50/60" : "border-stone-200"}`}>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 text-xs">
              {hasSelection ? (
                <>
                  <p className="font-medium text-stone-800">Выделено: {countLabel(selection.total, OBJECT_FORMS)}</p>
                  <p className="truncate text-stone-600">{describeSelection(selection)}</p>
                  {selection.single?.violation && <p className="text-red-700">Нарушен норматив отступа</p>}
                </>
              ) : (
                <p className="text-stone-500">Ничего не выделено</p>
              )}
            </div>
            {hasSelection ? (
              <button onClick={onClearSelection} className="flex items-center gap-1 rounded px-1 text-xs text-stone-500 hover:bg-white" title="Снять выделение">
                <X className="h-3.5 w-3.5" aria-hidden />
                <Kbd>Esc</Kbd>
              </button>
            ) : (
              <button
                onClick={onSelectAll}
                disabled={!hasPlan || !showPlan}
                className="rounded px-1 text-xs text-greenery-700 hover:bg-greenery-50 disabled:opacity-40"
              >
                Выделить всё <Kbd>Ctrl+A</Kbd>
              </button>
            )}
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Button variant="outline" size="sm" disabled={!hasSelection || !canEdit} onClick={() => onRetypeSelection("tree")}>
              <TreeDeciduous className="mr-1.5 h-4 w-4" aria-hidden />
              Дерево
              <Kbd className="ml-auto">1</Kbd>
            </Button>
            <Button variant="outline" size="sm" disabled={!hasSelection || !canEdit} onClick={() => onRetypeSelection("shrub")}>
              <Shrub className="mr-1.5 h-4 w-4" aria-hidden />
              Кустарник
              <Kbd className="ml-auto">2</Kbd>
            </Button>
          </div>
          <Button
            variant="outline"
            size="sm"
            disabled={!hasSelection || !canEdit}
            onClick={onDeleteSelection}
            className="border-red-200 text-red-700 hover:bg-red-50"
          >
            <Trash2 className="mr-1.5 h-4 w-4" aria-hidden />
            Удалить
            <Kbd className="ml-auto">Del</Kbd>
          </Button>
        </div>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">Отображение на карте</h2>
        {layerLegend.length > 0 && (
          <ul className="flex flex-col gap-1.5">
            {layerLegend.map((entry) => (
              <li key={entry.key}>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={!hiddenLayerTypes.has(entry.key)}
                    onChange={() => onToggleLayerType(entry.key)}
                  />
                  <span
                    className="h-3 w-3 flex-none rounded-sm border border-black/10"
                    style={{ backgroundColor: entry.color }}
                    aria-hidden
                  />
                  {entry.label}
                  <span className="text-xs text-stone-400">{entry.count}</span>
                </label>
              </li>
            ))}
          </ul>
        )}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={showPlan} onChange={(e) => onToggleShowPlan(e.target.checked)} />
          Сгенерированный план
        </label>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">3. Нормативы и экспорт</h2>
          {validating && hasPlan && (
            <span className="flex items-center gap-1 text-xs text-stone-500">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Проверка…
            </span>
          )}
        </div>
        {hasPlan && violations !== null && violations.length === 0 && (
          <p className="text-xs text-greenery-700">Нарушений отступов не найдено.</p>
        )}
        {hasPlan && violations !== null && violations.length > 0 && (
          <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-800">
            <p className="flex items-center gap-1.5 font-medium">
              <span className="inline-block h-2.5 w-2.5 flex-none rounded-full bg-red-600" aria-hidden />
              Нарушений: {violations.length} — отмечены на карте красным
            </p>
            <ul className="mt-1.5 flex flex-col gap-1">
              {violationGroups.map(([message, ids]) => {
                const clicks = violationCursor[message] ?? 0;
                return (
                  <li key={message} className="flex items-center justify-between gap-2">
                    <span className="min-w-0">
                      {message} <span className="text-red-600">× {ids.length}</span>
                    </span>
                    <button
                      disabled={!showPlan}
                      onClick={() => {
                        onFocusItem(ids[clicks % ids.length]);
                        setViolationCursor((c) => ({ ...c, [message]: clicks + 1 }));
                      }}
                      className="flex-none rounded border border-red-200 bg-white px-1.5 py-0.5 text-red-700 hover:bg-red-100 disabled:opacity-40"
                    >
                      {clicks === 0 || ids.length === 1 ? "Показать" : `Следующее ${(clicks % ids.length) + 1}/${ids.length}`}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <Button
          variant="outline"
          disabled={!hasPlan || busy || exporting}
          className="w-full"
          onClick={() => guarded(() => onExportDxf())}
        >
          {exporting && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden />}
          {exporting ? "Экспортируем…" : "Экспорт в DXF"}
        </Button>
      </section>
    </aside>
  );
}
