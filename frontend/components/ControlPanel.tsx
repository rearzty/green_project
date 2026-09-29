"use client";

import { useMemo, useState } from "react";
import { Box, Loader2, Redo2, Shrub, SquareDashedMousePointer, Trash2, TreeDeciduous, Undo2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import type { ItemCompliance, PlanSummary, PlantingNorms, PlantingType, ScoringMode, ValidationViolation } from "@/lib/api";
import { countLabel, OBJECT_FORMS, PLANTING_TYPE_FORMS, PLANTING_TYPE_LABELS } from "@/lib/format";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import { SEASON_LABELS, type LayerLegendEntry, type Season } from "@/lib/mapStyle";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const CRS_PRESETS = [
  { value: "EPSG:4326", label: "WGS84 (EPSG:4326)" },
  { value: "EPSG:3857", label: "Web Mercator (EPSG:3857)" },
  { value: "EPSG:32636", label: "UTM 36N (EPSG:32636)" },
  { value: "EPSG:32637", label: "UTM 37N (EPSG:32637)" },
] as const;
const CUSTOM_CRS = "__custom__";

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
  onGenerate: (
    plantingTypes: PlantingType[],
    scoringMode: ScoringMode,
    spacing?: { treeSpacingM?: number; shrubSpacingM?: number }
  ) => Promise<void>;
  /** Lifted to app/page.tsx (not local state here anymore) so AssistantChat.tsx
   * can read/change the same recipe Юна talks about -- both this panel and
   * the chat now control one shared value instead of each holding their own
   * copy that could silently disagree with what the other last generated. */
  plantingTypes: PlantingType[];
  onPlantingTypesChange: (types: PlantingType[]) => void;
  scoringMode: ScoringMode;
  onScoringModeChange: (mode: ScoringMode) => void;
  treeSpacingM?: number;
  onTreeSpacingMChange: (value: number | undefined) => void;
  shrubSpacingM?: number;
  onShrubSpacingMChange: (value: number | undefined) => void;
  /** Current planting-norms defaults (GET /api/config/planting-norms),
   * fetched once in app/page.tsx -- shown as the interval inputs'
   * placeholder so the panel never hardcodes a number that could drift from
   * planting_norms.yaml (same reasoning ThreeDView.tsx already follows for
   * canopy sizing). Undefined until the fetch resolves. */
  plantingNorms?: PlantingNorms;
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
  /** Read-only 3D view (components/ThreeDView.tsx) instead of the 2D map --
   * mutually exclusive with it, not an overlay. */
  view3d: boolean;
  onToggleView3d: () => void;
  /** Cosmetic 3D-only tint (ThreeDView.tsx) -- disabled in the UI while
   * view3d is false, but always rendered so toggling 3D doesn't shift the
   * panel's layout. */
  season: Season;
  onSeasonChange: (season: Season) => void;
  /** Every plan generated so far for the current project. */
  plans: PlanSummary[];
  currentPlanId?: string;
  onSelectPlan: (planId: string) => Promise<void>;
  /** Permanently removes a plan from history -- no undo. The backend
   * refuses to delete the project's `is_current` plan (409); the panel
   * disables that row's delete button rather than surfacing the error. */
  onDeletePlan: (planId: string) => Promise<void>;

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

  /** Normative justification for the single selected item ("Почему здесь?") --
   * fetched in app/page.tsx whenever exactly one item is selected (see
   * lib/api.ts::getItemsCompliance). undefined while loading/not applicable
   * (more than one item selected, or none), an object once it resolves. This
   * is the same geo_engine/compliance.py output the CLI already writes to its
   * JSON report — the brief's traceability requirement, not a nicety. */
  complianceForSelection?: ItemCompliance | null;
  complianceLoading?: boolean;
  /** Download the whole plan's justification as JSON/CSV -- the CLI's
   * report.json/--csv, over HTTP. Undefined (no button) before a plan exists. */
  onDownloadComplianceReport?: (format: "json" | "csv") => void;
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
  plantingTypes,
  onPlantingTypesChange,
  scoringMode,
  onScoringModeChange,
  treeSpacingM,
  onTreeSpacingMChange,
  shrubSpacingM,
  onShrubSpacingMChange,
  plantingNorms,
  onExportDxf,
  onClearAll,
  layerLegend,
  hiddenLayerTypes,
  onToggleLayerType,
  showPlan,
  onToggleShowPlan,
  view3d,
  onToggleView3d,
  season,
  onSeasonChange,
  plans,
  currentPlanId,
  onSelectPlan,
  onDeletePlan,
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
  complianceForSelection,
  complianceLoading = false,
  onDownloadComplianceReport,
}: ControlPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("Тестовая территория");
  // Defaults to the CRS scripts/generate_synthetic_data.py writes test data
  // in (UTM 37N, covers Moscow) so synthetic uploads land on the real map
  // out of the box — clear/replace it once real Mosgeotrest data (unknown
  // CRS until 15.09) is being uploaded instead.
  const [sourceCrs, setSourceCrs] = useState("EPSG:32637");
  const [crsPreset, setCrsPreset] = useState<string>("EPSG:32637");
  const [busy, setBusy] = useState(false);
  // Per violation group: how many times "show on map" was clicked, to step through its items.
  const [violationCursor, setViolationCursor] = useState<Record<string, number>>({});
  // Plan-history delete is permanent (no undo, unlike item edits) -- a
  // second click within the same row confirms it, rather than a native
  // window.confirm() popup (this app avoids native dialogs elsewhere too,
  // see toast.ts). Blurring the button (click elsewhere) resets it.
  const [confirmDeletePlanId, setConfirmDeletePlanId] = useState<string | null>(null);

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
    onPlantingTypesChange(plantingTypes.includes(type) ? plantingTypes.filter((t) => t !== type) : [...plantingTypes, type]);
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
    <aside className="flex h-full w-[85vw] max-w-80 flex-none flex-col gap-4 overflow-y-auto border-r border-stone-700 bg-stone-900 p-3 text-stone-50 md:p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold text-greenery-300">GreenProject</h1>
          <p className="text-xs text-stone-400">Автопроектирование озеленения территории</p>
        </div>
        {hasProject && (
          <button
            onClick={onClearAll}
            title="Забыть текущий проект в этом браузере (в базе данных ничего не удаляется)"
            className="whitespace-nowrap rounded border border-stone-600 px-2 py-1 text-xs text-stone-300 hover:bg-stone-800"
          >
            Очистить всё
          </button>
        )}
      </div>
      {restoring && <p className="text-xs text-stone-400">Восстановление сессии…</p>}

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium">1. Загрузить чертёж</h2>
        {/* Подсказка стоит ПЕРЕД выбором файла, а не после кнопки, как было:
            пользователь читал её уже после того, как ему пришлось решить, что
            класть. И формулировка теперь говорит, что СДЕЛАТЬ, а не из чего
            состоит поставка: «заархивируйте папку улицы» вместо «вся папка
            объекта (чертёж + Xrefs/)» — второе понятно только тому, кто и так
            знает устройство данных.

            Почему именно zip, а не один файл: граница участка на реальных
            поставках лежит не в главном чертеже, а во внешних ссылках рядом
            (см. CLAUDE.md), и без них план построить нельзя. */}
        <p className="text-xs text-stone-400">
          Заархивируйте папку улицы целиком в <span className="text-stone-300">.zip</span> и выберите его
          ниже — в ней должен быть главный чертёж и папка внешних ссылок рядом
          (<span className="text-stone-300">Xrefs</span>, <span className="text-stone-300">Ссылки</span> и т. п.).
          Граница участка обычно лежит именно в них, поэтому одного файла не хватит.
        </p>
        <input
          type="file"
          accept=".dxf,.dwg,.zip,.geojson,.json"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-sm text-stone-300 file:mr-3 file:cursor-pointer file:rounded-md file:border-0 file:bg-greenery-600 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-white hover:file:bg-greenery-700"
        />
        <p className="text-xs text-stone-500">
          Можно и один <span className="text-stone-400">.dwg</span> или <span className="text-stone-400">.dxf</span>,
          если граница участка есть прямо в нём.
        </p>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Название — как отличить этот проект от других"
          className="rounded border border-stone-600 bg-stone-900 px-2 py-1 text-sm"
        />
        {/* Система координат ушла ПОСЛЕ файла и подписана как необязательная.
            Раньше она стояла вторым шагом, перед выбором файла, и читалась как
            обязательная — хотя трогать её почти никогда не нужно: подтверждение
            CRS выводится из самих координат файла, а не из этого поля. */}
        <details className="text-xs text-stone-400">
          <summary className="cursor-pointer select-none hover:text-stone-300">
            Система координат — обычно менять не нужно
          </summary>
          <div className="mt-2 flex flex-col gap-2">
            <p className="text-stone-500">
              По умолчанию определяется по самому файлу. Указывайте вручную, только если точно её знаете.
            </p>
            <select
              value={crsPreset}
              onChange={(e) => {
                setCrsPreset(e.target.value);
                if (e.target.value !== CUSTOM_CRS) setSourceCrs(e.target.value);
              }}
              className="rounded border border-stone-600 bg-stone-900 px-2 py-1 text-sm"
            >
              {CRS_PRESETS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
              <option value={CUSTOM_CRS}>Свой…</option>
            </select>
            {crsPreset === CUSTOM_CRS && (
              <input
                type="text"
                value={sourceCrs}
                onChange={(e) => setSourceCrs(e.target.value)}
                placeholder="напр. EPSG:32637"
                className="rounded border border-stone-600 bg-stone-900 px-2 py-1 text-sm"
              />
            )}
          </div>
        </details>
        <Button disabled={!file || busy} onClick={() => file && guarded(() => onUpload(file, name, sourceCrs))}>
          {busy ? "Загружаю…" : "Загрузить чертёж"}
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-700 pt-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">2. Сгенерировать план</h2>
          {generating && (
            <span className="flex items-center gap-1 text-xs text-stone-400">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Генерация…
            </span>
          )}
        </div>
        <div className="flex flex-col gap-1 text-sm">
          {(Object.keys(GENERATE_TYPE_LABELS) as PlantingType[]).map((type) => (
            <label key={type} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={plantingTypes.includes(type)}
                onChange={() => toggleType(type)}
                className="h-4 w-4 accent-greenery-600"
              />
              {GENERATE_TYPE_LABELS[type]}
            </label>
          ))}
        </div>
        <div className="flex flex-col gap-1 text-sm">
          <label className="flex items-center gap-2">
            <span className="w-32 flex-none text-stone-300">Деревья: интервал, м</span>
            <input
              type="number"
              step={0.5}
              min={0.5}
              max={15}
              value={treeSpacingM ?? ""}
              onChange={(e) => onTreeSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
              disabled={!plantingTypes.includes("tree")}
              placeholder={String(plantingNorms?.species_spacing.tree_default?.min_distance_m ?? 5)}
              className="w-20 rounded border border-stone-600 bg-stone-900 px-2 py-0.5 disabled:opacity-50"
            />
          </label>
          <label className="flex items-center gap-2">
            <span className="w-32 flex-none text-stone-300">Кусты: интервал, м</span>
            <input
              type="number"
              step={0.5}
              min={0.3}
              max={10}
              value={shrubSpacingM ?? ""}
              onChange={(e) => onShrubSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
              disabled={!plantingTypes.includes("shrub")}
              placeholder={String(plantingNorms?.species_spacing.shrub_default?.min_distance_m ?? 3)}
              className="w-20 rounded border border-stone-600 bg-stone-900 px-2 py-0.5 disabled:opacity-50"
            />
          </label>
        </div>
        <div className="flex gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "heuristic"} onChange={() => onScoringModeChange("heuristic")} className="h-4 w-4 accent-greenery-600" />
            Эвристика
          </label>
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "ml"} onChange={() => onScoringModeChange("ml")} className="h-4 w-4 accent-greenery-600" />
            ML
          </label>
        </div>
        <Button
          disabled={!hasProject || plantingTypes.length === 0 || busy || editBusy || generating}
          onClick={() => guarded(() => onGenerate(plantingTypes, scoringMode, { treeSpacingM, shrubSpacingM }))}
        >
          Сгенерировать план
        </Button>
      </section>

      {plans.length > 0 && (
        <section className="flex flex-col gap-2 border-t border-stone-700 pt-3">
          <h2 className="text-sm font-medium">История планов</h2>
          <ul className="flex max-h-32 flex-col gap-1 overflow-y-auto text-xs">
            {plans.map((p) => (
              <li key={p.plan_id} className="flex items-center gap-1">
                <button
                  disabled={busy || editBusy}
                  onClick={() => guarded(() => onSelectPlan(p.plan_id))}
                  className={`w-full min-w-0 rounded border px-2 py-1 text-left ${
                    p.plan_id === currentPlanId ? "border-greenery-500 bg-stone-800" : "border-stone-700"
                  }`}
                >
                  {formatPlanLabel(p)}
                </button>
                <button
                  disabled={busy || editBusy || p.is_current}
                  onClick={() => {
                    if (confirmDeletePlanId === p.plan_id) {
                      setConfirmDeletePlanId(null);
                      guarded(() => onDeletePlan(p.plan_id));
                    } else {
                      setConfirmDeletePlanId(p.plan_id);
                    }
                  }}
                  onBlur={() => setConfirmDeletePlanId((id) => (id === p.plan_id ? null : id))}
                  title={p.is_current ? "Нельзя удалить текущий план" : confirmDeletePlanId === p.plan_id ? "Точно удалить?" : "Удалить план"}
                  className={`flex-none rounded p-1 disabled:opacity-30 ${
                    confirmDeletePlanId === p.plan_id ? "bg-red-900 text-red-200" : "text-stone-400 hover:bg-red-950/60 hover:text-red-300"
                  }`}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="flex flex-col gap-2 border-t border-stone-700 pt-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">Правка плана</h2>
          {editBusy && (
            <span className="flex items-center gap-1 text-xs text-stone-400">
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

        <p className="text-xs text-stone-400">
          {selectMode
            ? "Потяните по карте — выделить рамкой (с Shift — добавить к выделению). Потяните выделенное — переместить."
            : "Клик по объекту — выделить (Shift — несколько), перетаскивание точки — переместить. Для рамки включите режим выделения."}{" "}
          ПКМ — меню действий.
        </p>

        <div className={`flex flex-col gap-2 rounded-md border p-2 ${hasSelection ? "border-blue-900 bg-blue-950/40" : "border-stone-700"}`}>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 text-xs">
              {hasSelection ? (
                <>
                  <p className="font-medium text-stone-100">Выделено: {countLabel(selection.total, OBJECT_FORMS)}</p>
                  <p className="truncate text-stone-300">{describeSelection(selection)}</p>
                  {selection.single?.violation && <p className="text-red-400">Нарушен норматив отступа</p>}
                  {selection.single && (
                    <div className="mt-1.5 border-t border-stone-700 pt-1.5">
                      <p className="font-medium text-stone-200">Почему здесь?</p>
                      {complianceLoading ? (
                        <p className="flex items-center gap-1 text-stone-400">
                          <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                          Считаю обоснование…
                        </p>
                      ) : complianceForSelection ? (
                        <p className="whitespace-pre-wrap text-stone-300">{complianceForSelection.summary}</p>
                      ) : (
                        <p className="text-stone-500">Обоснование недоступно.</p>
                      )}
                    </div>
                  )}
                </>
              ) : (
                <p className="text-stone-400">Ничего не выделено</p>
              )}
            </div>
            {hasSelection ? (
              <button onClick={onClearSelection} className="flex items-center gap-1 rounded px-1 text-xs text-stone-400 hover:bg-stone-800" title="Снять выделение">
                <X className="h-3.5 w-3.5" aria-hidden />
                <Kbd>Esc</Kbd>
              </button>
            ) : (
              <button
                onClick={onSelectAll}
                disabled={!hasPlan || !showPlan}
                className="rounded px-1 text-xs text-greenery-300 hover:bg-stone-800 disabled:opacity-40"
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
            className="border-red-900 text-red-300 hover:bg-red-950/60"
          >
            <Trash2 className="mr-1.5 h-4 w-4" aria-hidden />
            Удалить
            <Kbd className="ml-auto">Del</Kbd>
          </Button>
        </div>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-700 pt-3">
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
                    className="h-4 w-4 accent-greenery-600"
                  />
                  <span
                    className="h-3 w-3 flex-none rounded-sm border border-white/10"
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
          <input type="checkbox" checked={showPlan} onChange={(e) => onToggleShowPlan(e.target.checked)} className="h-4 w-4 accent-greenery-600" />
          Сгенерированный план
        </label>
        <Button
          variant={view3d ? "default" : "outline"}
          size="sm"
          disabled={!view3d && !hasPlan}
          onClick={onToggleView3d}
          aria-pressed={view3d}
          title="Только просмотр — без выделения и правки"
        >
          <Box className="mr-1.5 h-4 w-4" aria-hidden />
          {view3d ? "Вернуться на 2D-карту" : "Показать в 3D"}
        </Button>
        <div className="grid grid-cols-4 gap-1">
          {(Object.keys(SEASON_LABELS) as Season[]).map((s) => (
            <button
              key={s}
              disabled={!view3d}
              onClick={() => onSeasonChange(s)}
              aria-pressed={season === s}
              className={cn(
                "rounded border px-1 py-1 text-xs disabled:opacity-40",
                season === s ? "border-greenery-500 bg-stone-800 text-greenery-300" : "border-stone-700 text-stone-300 hover:bg-stone-800"
              )}
            >
              {SEASON_LABELS[s]}
            </button>
          ))}
        </div>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-700 pt-3">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">3. Нормативы и экспорт</h2>
          {validating && hasPlan && (
            <span className="flex items-center gap-1 text-xs text-stone-400">
              <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              Проверка…
            </span>
          )}
        </div>
        {hasPlan && violations !== null && violations.length === 0 && (
          <p className="text-xs text-greenery-300">Нарушений отступов не найдено.</p>
        )}
        {hasPlan && violations !== null && violations.length > 0 && (
          <div className="rounded-md border border-red-900 bg-red-950/60 p-2 text-xs text-red-300">
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
                      {message} <span className="text-red-400">× {ids.length}</span>
                    </span>
                    <button
                      disabled={!showPlan}
                      onClick={() => {
                        onFocusItem(ids[clicks % ids.length]);
                        setViolationCursor((c) => ({ ...c, [message]: clicks + 1 }));
                      }}
                      className="flex-none rounded border border-red-900 bg-stone-900 px-1.5 py-0.5 text-red-300 hover:bg-red-950 disabled:opacity-40"
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
        {/* Полное обоснование по НПА для всего плана -- то же, что CLI пишет
            в report.json/--csv (geo_engine/compliance.py), но по HTTP.
            Обязательное требование ТЗ (ссылка на акт и пункт для каждой
            посадки), не бонус — поэтому кнопка всегда рядом с экспортом, а
            не спрятана. */}
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!hasPlan || !onDownloadComplianceReport}
            className="flex-1"
            onClick={() => onDownloadComplianceReport?.("json")}
            title="Обоснование каждой посадки со ссылкой на акт и пункт (JSON)"
          >
            Обоснование (JSON)
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!hasPlan || !onDownloadComplianceReport}
            className="flex-1"
            onClick={() => onDownloadComplianceReport?.("csv")}
            title="То же самое построчно: посадка × норматив (CSV)"
          >
            Обоснование (CSV)
          </Button>
        </div>
      </section>
    </aside>
  );
}
