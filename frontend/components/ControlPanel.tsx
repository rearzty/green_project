"use client";

import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Box,
  ChevronDown,
  Download,
  FileArchive,
  FileJson,
  FileSpreadsheet,
  Leaf,
  Loader2,
  Redo2,
  RotateCcw,
  SquareDashedMousePointer,
  Trash2,
  Undo2,
  Upload,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import type { AreaBudget, PlanSummary, PlantingType, ScoringMode, ValidationViolation } from "@/lib/api";
import type { JobProgress } from "@/lib/hooks/useProjectSession";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import { PLANTING_COLORS, SEASON_LABELS, type LayerLegendEntry, type Season } from "@/lib/mapStyle";
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
  shrub: "Кустарник",
  lawn: "Газон",
};

function formatPlanTime(plan: PlanSummary): string {
  return new Date(plan.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/** Stage text + a fill bar for a long-running background job (upload/
 * generate) -- without this, a job that can genuinely take 100+ seconds
 * reads as a hung UI, not a busy one. `progress` null (no countable total
 * yet) renders as an animated indeterminate stripe instead of a stuck 0%. */
function ProgressBar({ label, progress }: { label: string; progress: number | null }) {
  const pct = progress !== null ? Math.round(Math.min(1, Math.max(0, progress)) * 100) : null;
  return (
    <div className="flex flex-col gap-1">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-stone-700">
        {pct !== null ? (
          <div className="h-full rounded-full bg-greenery-500 transition-[width] duration-300" style={{ width: `${pct}%` }} />
        ) : (
          <div className="h-full w-1/3 animate-[progress-indeterminate_1.2s_ease-in-out_infinite] rounded-full bg-greenery-500" />
        )}
      </div>
      <p className="text-xs text-stone-400">
        {label}
        {pct !== null ? ` — ${pct}%` : ""}
      </p>
    </div>
  );
}

/** One collapsible step of the panel. The header stays visible when
 * collapsed and can carry a one-line status, so a finished step reads as
 * "done: what" instead of disappearing. */
function Section({
  step,
  title,
  status,
  open,
  onToggle,
  disabled = false,
  children,
}: {
  step?: number;
  title: string;
  status?: React.ReactNode;
  open: boolean;
  onToggle: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className={cn("rounded-xl border border-stone-800 bg-stone-900/60", disabled && "opacity-50")}>
      <button
        onClick={onToggle}
        disabled={disabled}
        aria-expanded={open}
        className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left disabled:cursor-not-allowed"
      >
        {step !== undefined && (
          <span className="flex h-5 w-5 flex-none items-center justify-center rounded-full bg-stone-800 text-[11px] font-semibold text-stone-300">
            {step}
          </span>
        )}
        <span className="flex-1 text-sm font-medium text-stone-100">{title}</span>
        {status && <span className="min-w-0 max-w-[50%] truncate text-xs text-stone-400">{status}</span>}
        <ChevronDown className={cn("h-4 w-4 flex-none text-stone-500 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && !disabled && <div className="flex flex-col gap-3 border-t border-stone-800 px-3 pb-3 pt-3">{children}</div>}
    </section>
  );
}

function Toggle({ checked, onChange, disabled, children, title }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; children: React.ReactNode; title?: string }) {
  return (
    <label className={cn("flex items-start gap-2 text-sm text-stone-200", disabled && "opacity-50")} title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 h-4 w-4 flex-none accent-greenery-600" />
      <span>{children}</span>
    </label>
  );
}

export interface ControlPanelProps {
  hasProject: boolean;
  projectName?: string;
  /** Upload warnings of the current project (ProjectOut.warnings). */
  projectWarnings?: string[];
  /** The delivery has no work-area boundary, only the survey order boundary,
   * and the user hasn't agreed to plan by it yet -- ask before generating. */
  needsOrderBoundaryConsent?: boolean;
  /** Показать тот же вопрос о границе заказа, что по кнопке генерации, —
   * когда план попросили построить не кнопкой, а Юне (app/page.tsx).
   * Каждое новое значение — новый вопрос; 0 — не спрашивали. */
  consentRequest?: number;
  /** PlanOut.area_budget of the shown plan -- why a type got few plantings. */
  areaBudget?: AreaBudget[];
  hasPlan: boolean;
  /** True only during the brief client-side restore-from-localStorage pass. */
  restoring?: boolean;
  generating?: boolean;
  uploadProgress?: JobProgress | null;
  generateProgress?: JobProgress | null;
  exporting?: boolean;
  onUpload: (file: File, name: string, sourceCrs: string) => Promise<void>;
  onGenerate: (
    plantingTypes: PlantingType[],
    scoringMode: ScoringMode,
    spacing?: { treeSpacingM?: number; shrubSpacingM?: number; treeRows?: number; treesInPaving?: boolean; useOrderBoundary?: boolean }
  ) => Promise<void>;
  /** Lifted to app/page.tsx so AssistantChat.tsx reads/changes the same
   * recipe Юна talks about. */
  plantingTypes: PlantingType[];
  onPlantingTypesChange: (types: PlantingType[]) => void;
  scoringMode: ScoringMode;
  onScoringModeChange: (mode: ScoringMode) => void;
  treeSpacingM?: number;
  onTreeSpacingMChange: (value: number | undefined) => void;
  shrubSpacingM?: number;
  onShrubSpacingMChange: (value: number | undefined) => void;
  treeRows: number;
  onTreeRowsChange: (rows: number) => void;
  treesInPaving: boolean;
  onTreesInPavingChange: (value: boolean) => void;
  onExportDxf: () => Promise<void>;
  /** Forgets the locally-remembered project/plan; deletes nothing server-side. */
  onClearAll: () => void;
  layerLegend: LayerLegendEntry[];
  hiddenLayerTypes: ReadonlySet<string>;
  onToggleLayerType: (key: string) => void;
  showPlan: boolean;
  onToggleShowPlan: (show: boolean) => void;
  view3d: boolean;
  onToggleView3d: () => void;
  season: Season;
  onSeasonChange: (season: Season) => void;
  plans: PlanSummary[];
  currentPlanId?: string;
  onSelectPlan: (planId: string) => Promise<void>;
  /** Permanent -- the backend refuses the current plan (409), so its delete
   * button is disabled rather than surfacing the error. */
  onDeletePlan: (planId: string) => Promise<void>;

  editBusy: boolean;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  selectMode: boolean;
  onToggleSelectMode: () => void;
  /** Only the count is shown here -- the selection's details and actions
   * live in InspectorCard over the map. */
  selection: SelectionSummary | null;
  onSelectAll: () => void;

  validating: boolean;
  /** null = not checked yet for the current plan. */
  violations: ValidationViolation[] | null;
  onFocusItem: (itemId: string) => void;
  onDownloadComplianceReport?: (format: "json" | "csv") => void;
}

type SectionKey = "upload" | "generate" | "history" | "edit" | "view" | "export";

export function ControlPanel({
  hasProject,
  projectName,
  projectWarnings = [],
  needsOrderBoundaryConsent = false,
  consentRequest = 0,
  areaBudget = [],
  hasPlan,
  restoring = false,
  generating = false,
  uploadProgress = null,
  generateProgress = null,
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
  treeRows,
  onTreeRowsChange,
  treesInPaving,
  onTreesInPavingChange,
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
  onSelectAll,
  validating,
  violations,
  onFocusItem,
  onDownloadComplianceReport,
}: ControlPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [askOrderBoundary, setAskOrderBoundary] = useState(false);
  // Defaults to the CRS scripts/generate_synthetic_data.py writes test data
  // in; real DXF/DWG carries no CRS, and the backend ignores this for them
  // unless the user sets it explicitly (see project_service.py).
  const [sourceCrs, setSourceCrs] = useState("EPSG:32637");
  const [crsPreset, setCrsPreset] = useState<string>("EPSG:32637");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  // Per violation group: how many times "show on map" was clicked, to step through its items.
  const [violationCursor, setViolationCursor] = useState<Record<string, number>>({});
  // Plan-history delete is permanent -- a second click on the same row
  // confirms it (no native window.confirm(), see toast.ts).
  const [confirmDeletePlanId, setConfirmDeletePlanId] = useState<string | null>(null);

  // Which steps are open follows the workflow stage (upload first, then
  // generation, then editing/export) until the user toggles one by hand;
  // reaching a new stage resets the manual choices.
  const [openOverrides, setOpenOverrides] = useState<Partial<Record<SectionKey, boolean>>>({});
  const [uploadFormOpen, setUploadFormOpen] = useState(false);
  useEffect(() => {
    setOpenOverrides({});
    setUploadFormOpen(false);
  }, [hasProject, hasPlan]);
  const defaultOpen: Record<SectionKey, boolean> = {
    upload: !hasProject,
    generate: hasProject,
    history: false,
    edit: hasPlan,
    view: false,
    export: hasPlan,
  };
  const isOpen = (key: SectionKey) => openOverrides[key] ?? defaultOpen[key];
  const toggle = (key: SectionKey) => setOpenOverrides((o) => ({ ...o, [key]: !isOpen(key) }));

  // Согласие — только здесь, одним и тем же вопросом: Юна план по границе
  // заказа сама не строит, а открывает этот вопрос с её настройками в полях.
  useEffect(() => {
    if (consentRequest === 0 || !needsOrderBoundaryConsent) return;
    setAskOrderBoundary(true);
    setOpenOverrides((o) => ({ ...o, generate: true }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [consentRequest]);

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

  const canEdit = hasPlan && !editBusy;
  const showUploadForm = !hasProject || uploadFormOpen;
  const currentPlan = plans.find((p) => p.plan_id === currentPlanId);

  const uploadForm = (
    <>
      <p className="text-xs leading-relaxed text-stone-400">
        Заархивируйте папку улицы целиком в <span className="text-stone-200">.zip</span>: главный чертёж и папка внешних
        ссылок рядом (<span className="text-stone-200">Xrefs</span>, <span className="text-stone-200">Ссылки</span> и т. п.).
        Граница участка обычно лежит именно в ссылках.
      </p>
      <label
        className={cn(
          "flex cursor-pointer flex-col items-center gap-1.5 rounded-lg border border-dashed px-3 py-4 text-center transition-colors",
          file ? "border-greenery-600 bg-greenery-700/10" : "border-stone-700 hover:border-stone-500 hover:bg-stone-800/50"
        )}
      >
        <FileArchive className={cn("h-6 w-6", file ? "text-greenery-300" : "text-stone-500")} aria-hidden />
        <span className="max-w-full truncate text-sm text-stone-200">{file ? file.name : "Выберите .zip, .dwg или .dxf"}</span>
        <span className="text-[11px] text-stone-500">{file ? `${(file.size / 1024 / 1024).toFixed(1)} МБ` : "один чертёж — если граница участка в нём"}</span>
        <input
          type="file"
          accept=".dxf,.dwg,.zip,.geojson,.json"
          className="sr-only"
          onChange={(e) => {
            const picked = e.target.files?.[0] ?? null;
            setFile(picked);
            if (picked && !name) setName(picked.name.replace(/\.[^.]+$/, ""));
          }}
        />
      </label>
      <input
        type="text"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Название проекта"
        className="rounded-md border border-stone-700 bg-stone-950 px-2.5 py-1.5 text-sm placeholder:text-stone-600 focus:border-greenery-600 focus:outline-none"
      />
      <details className="text-xs text-stone-400">
        <summary className="cursor-pointer select-none hover:text-stone-300">Система координат — обычно менять не нужно</summary>
        <div className="mt-2 flex flex-col gap-2">
          <p className="text-stone-500">Определяется по самому файлу. Указывайте вручную, только если точно её знаете.</p>
          <select
            value={crsPreset}
            onChange={(e) => {
              setCrsPreset(e.target.value);
              if (e.target.value !== CUSTOM_CRS) setSourceCrs(e.target.value);
            }}
            className="rounded-md border border-stone-700 bg-stone-950 px-2 py-1 text-sm"
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
              className="rounded-md border border-stone-700 bg-stone-950 px-2 py-1 text-sm"
            />
          )}
        </div>
      </details>
      <Button
        disabled={!file || busy}
        onClick={() =>
          file &&
          guarded(async () => {
            setUploading(true);
            try {
              await onUpload(file, name || file.name, sourceCrs);
            } finally {
              setUploading(false);
            }
          })
        }
      >
        {uploading ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden /> : <Upload className="mr-1.5 h-4 w-4" aria-hidden />}
        {uploading ? "Загружаю…" : "Загрузить"}
      </Button>
      {uploadProgress && <ProgressBar label={uploadProgress.stage} progress={uploadProgress.progress} />}
    </>
  );

  return (
    <aside className="flex h-full w-[88vw] max-w-96 flex-none flex-col border-r border-stone-800 bg-stone-950 text-stone-50">
      <header className="flex items-center justify-between gap-2 border-b border-stone-800 px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-greenery-600/20">
            <Leaf className="h-4 w-4 text-greenery-300" aria-hidden />
          </span>
          <div>
            <h1 className="text-base font-semibold leading-tight text-stone-50">GreenProject</h1>
            <p className="text-[11px] leading-tight text-stone-400">Автопроектирование озеленения</p>
          </div>
        </div>
        {hasProject && (
          <button
            onClick={onClearAll}
            title="Забыть текущий проект в этом браузере (на сервере ничего не удаляется)"
            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-stone-400 hover:bg-stone-800 hover:text-stone-200"
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
            Новый проект
          </button>
        )}
      </header>

      <div className="flex flex-1 flex-col gap-2.5 overflow-y-auto p-3">
        {restoring && <p className="text-xs text-stone-400">Восстановление сессии…</p>}

        <Section step={1} title="Чертёж" status={hasProject ? projectName : undefined} open={isOpen("upload")} onToggle={() => toggle("upload")}>
          {hasProject && !showUploadForm ? (
            <div className="flex items-center justify-between gap-2">
              <p className="min-w-0 truncate text-sm text-stone-200" title={projectName}>
                {projectName}
              </p>
              <Button variant="secondary" size="sm" onClick={() => setUploadFormOpen(true)}>
                Другой чертёж
              </Button>
            </div>
          ) : (
            uploadForm
          )}
        </Section>

        {hasProject && projectWarnings.length > 0 && (
          <ul className="flex flex-col gap-1.5 rounded-xl border border-amber-800/60 bg-amber-950/30 p-2.5 text-xs text-amber-200">
            {projectWarnings.map((warning) => (
              <li key={warning} className="flex gap-1.5">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" aria-hidden />
                <span>{warning}</span>
              </li>
            ))}
          </ul>
        )}

        <Section
          step={2}
          title="Генерация плана"
          status={generating ? "идёт…" : currentPlan ? `${currentPlan.item_count.toLocaleString("ru-RU")} поз.` : undefined}
          open={isOpen("generate")}
          onToggle={() => toggle("generate")}
          disabled={!hasProject}
        >
          <div>
            <p className="mb-1.5 text-xs text-stone-400">Что сажать</p>
            <div className="grid grid-cols-3 gap-1.5">
              {(Object.keys(GENERATE_TYPE_LABELS) as PlantingType[]).map((type) => {
                const on = plantingTypes.includes(type);
                return (
                  <button
                    key={type}
                    onClick={() => toggleType(type)}
                    aria-pressed={on}
                    className={cn(
                      "flex items-center justify-center gap-1.5 rounded-md border px-2 py-1.5 text-xs transition-colors",
                      on ? "border-greenery-600 bg-greenery-700/20 text-stone-50" : "border-stone-700 text-stone-400 hover:bg-stone-800"
                    )}
                  >
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: on ? PLANTING_COLORS[type] : "#57534e" }} aria-hidden />
                    {GENERATE_TYPE_LABELS[type]}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1 text-xs text-stone-400">
              Интервал деревьев, м
              <input
                type="number"
                step={0.5}
                min={0.5}
                max={15}
                value={treeSpacingM ?? ""}
                onChange={(e) => onTreeSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
                disabled={!plantingTypes.includes("tree")}
                // По норме интервал задаёт класс кроны породы, а не одно число;
                // своё значение только раздвигает деревья (geo_engine/planner.py).
                placeholder="3–8 (по кроне)"
                title="По норме — по классу кроны: 8 м широкая, 5 м средняя, 3 м узкая (МГСН 1.02-02 п. 4.2.9.2). Своё значение может только раздвинуть деревья, ближе нормы их не поставить."
                className="rounded-md border border-stone-700 bg-stone-950 px-2 py-1 text-sm text-stone-100 placeholder:text-stone-600 disabled:opacity-50"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-stone-400">
              Интервал кустарника, м
              <input
                type="number"
                step={0.1}
                min={0.3}
                max={10}
                value={shrubSpacingM ?? ""}
                onChange={(e) => onShrubSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
                disabled={!plantingTypes.includes("shrub")}
                // Почти весь кустарник — живая изгородь, и интервал пользователя
                // заменяет именно её шаг (planner.IN_ROW_PITCH_M), а не 3 м
                // из planting_norms.yaml.
                placeholder="0,5 (изгородь)"
                title="Шаг кустарника в живой изгороди: по норме 0,5 м (743-ПП табл. 3.6.2). Куртины сажаются по плотности породы (515-ПП табл. 5)."
                className="rounded-md border border-stone-700 bg-stone-950 px-2 py-1 text-sm text-stone-100 placeholder:text-stone-600 disabled:opacity-50"
              />
            </label>
          </div>

          <div className="flex flex-col gap-1.5">
            <Toggle
              checked={treeRows === 2}
              onChange={(v) => onTreeRowsChange(v ? 2 : 1)}
              disabled={!plantingTypes.includes("tree")}
              title="743-ПП табл. 3.6.2: двухрядная посадка, интервал 7–8 м, второй ряд в шахматном порядке"
            >
              Двухрядная аллея вдоль улиц
            </Toggle>
            <Toggle
              checked={treesInPaving}
              onChange={onTreesInPavingChange}
              disabled={!plantingTypes.includes("tree")}
              title="743-ПП п. 9.8: околоствольное пространство Ø 2 м с приствольной решёткой; остальные отступы не меняются"
            >
              Деревья в покрытии тротуара <span className="text-stone-500">(решётка Ø 2 м)</span>
            </Toggle>
          </div>

          <div>
            <p className="mb-1.5 text-xs text-stone-400">Выбор лучших мест</p>
            <div className="grid grid-cols-2 rounded-md border border-stone-700 p-0.5 text-xs">
              {(["heuristic", "ml"] as ScoringMode[]).map((mode) => (
                <button
                  key={mode}
                  onClick={() => onScoringModeChange(mode)}
                  aria-pressed={scoringMode === mode}
                  title={
                    mode === "heuristic"
                      ? "Формула: запас по отступам, удалённость от существующей зелени"
                      : "Модель, обученная на посадочных чертежах проектировщиков 10 улиц пилота: из допустимых мест деревья ставятся туда, где их ставят проектировщики"
                  }
                  className={cn("rounded px-2 py-1 transition-colors", scoringMode === mode ? "bg-stone-700 text-stone-50" : "text-stone-400 hover:text-stone-200")}
                >
                  {mode === "heuristic" ? "Эвристика" : "ML-модель"}
                </button>
              ))}
            </div>
          </div>

          <Button
            disabled={!hasProject || plantingTypes.length === 0 || busy || editBusy || generating}
            onClick={() =>
              needsOrderBoundaryConsent
                ? setAskOrderBoundary(true)
                : guarded(() => onGenerate(plantingTypes, scoringMode, { treeSpacingM, shrubSpacingM, treeRows, treesInPaving }))
            }
          >
            {generating && <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden />}
            {generating ? "Генерация…" : hasPlan ? "Сгенерировать заново" : "Сгенерировать план"}
          </Button>
          {askOrderBoundary && needsOrderBoundaryConsent && (
            <div className="flex flex-col gap-2 rounded border border-amber-600/60 bg-amber-950/40 p-2 text-xs text-amber-100">
              <p className="font-medium">Границы работ не найдены — генерировать по границе заказа на съёмку?</p>
              <p className="text-amber-200/80">
                Граница заказа шире фактического участка работ: отступы от сетей и зданий соблюдаются, но посадки
                могут оказаться за участком. Результат предварительный.
              </p>
              <div className="flex gap-2">
                <Button
                  onClick={() => {
                    setAskOrderBoundary(false);
                    guarded(() =>
                      onGenerate(plantingTypes, scoringMode, {
                        treeSpacingM,
                        shrubSpacingM,
                        treeRows,
                        treesInPaving,
                        useOrderBoundary: true,
                      })
                    );
                  }}
                >
                  Да, по границе заказа
                </Button>
                <Button variant="outline" size="sm" onClick={() => setAskOrderBoundary(false)}>
                  Отмена
                </Button>
              </div>
            </div>
          )}
          {generateProgress && <ProgressBar label={generateProgress.stage} progress={generateProgress.progress} />}
          {hasPlan && areaBudget.length > 0 && <AreaBudgetList budgets={areaBudget} />}
        </Section>

        {plans.length > 1 && (
          <Section title="Версии плана" status={`${plans.length}`} open={isOpen("history")} onToggle={() => toggle("history")}>
            <ul className="flex max-h-44 flex-col gap-1 overflow-y-auto text-xs">
              {plans.map((p) => (
                <li key={p.plan_id} className="flex items-center gap-1">
                  <button
                    disabled={busy || editBusy}
                    onClick={() => guarded(() => onSelectPlan(p.plan_id))}
                    className={cn(
                      "flex w-full min-w-0 items-center justify-between gap-2 rounded-md border px-2 py-1.5 text-left",
                      p.plan_id === currentPlanId ? "border-greenery-600 bg-greenery-700/10" : "border-stone-800 hover:bg-stone-800/60"
                    )}
                  >
                    <span className="truncate text-stone-200">
                      {p.scoring_mode === "ml" ? "ML" : "Эвристика"} · {formatPlanTime(p)}
                      {p.is_current && <span className="ml-1 text-greenery-300">· последний</span>}
                    </span>
                    <span className="flex-none tabular-nums text-stone-400">{p.item_count.toLocaleString("ru-RU")}</span>
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
                    title={p.is_current ? "Нельзя удалить последний план" : confirmDeletePlanId === p.plan_id ? "Точно удалить?" : "Удалить план"}
                    className={cn(
                      "flex-none rounded p-1.5 disabled:opacity-30",
                      confirmDeletePlanId === p.plan_id ? "bg-red-900 text-red-200" : "text-stone-500 hover:bg-red-950/60 hover:text-red-300"
                    )}
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          </Section>
        )}

        <Section
          step={3}
          title="Ручная правка"
          status={editBusy ? "сохранение…" : selection ? `выделено ${selection.total}` : undefined}
          open={isOpen("edit")}
          onToggle={() => toggle("edit")}
          disabled={!hasPlan}
        >
          <div className="grid grid-cols-2 gap-1.5">
            <Button variant="secondary" size="sm" className="justify-between" disabled={!canUndo || !canEdit} onClick={onUndo} title="Отменить последнюю правку">
              <span className="flex items-center gap-1.5">
                <Undo2 className="h-3.5 w-3.5" aria-hidden />
                Отменить
              </span>
              <Kbd>Ctrl+Z</Kbd>
            </Button>
            <Button variant="secondary" size="sm" className="justify-between" disabled={!canRedo || !canEdit} onClick={onRedo} title="Повторить отменённую правку">
              <span className="flex items-center gap-1.5">
                <Redo2 className="h-3.5 w-3.5" aria-hidden />
                Повторить
              </span>
              <Kbd>Ctrl+Y</Kbd>
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            <Button
              variant={selectMode ? "default" : "secondary"}
              size="sm"
              className="justify-between"
              disabled={!selectMode && !showPlan}
              onClick={onToggleSelectMode}
              aria-pressed={selectMode}
              title="Левой кнопкой по карте — рамка выделения вместо перемещения карты"
            >
              <span className="flex items-center gap-1.5">
                <SquareDashedMousePointer className="h-3.5 w-3.5" aria-hidden />
                Рамка
              </span>
              <Kbd className={selectMode ? "border-greenery-500 bg-greenery-700 text-white" : ""}>S</Kbd>
            </Button>
            <Button variant="secondary" size="sm" className="justify-between" disabled={!showPlan} onClick={onSelectAll}>
              Выделить всё
              <Kbd>Ctrl+A</Kbd>
            </Button>
          </div>
          <p className="text-xs leading-relaxed text-stone-500">
            {selectMode
              ? "Протяните по карте — выделить рамкой (Shift — добавить). Потяните выделенное — переместить."
              : "Клик — выделить (Shift — несколько), перетаскивание — переместить. ПКМ — меню действий."}{" "}
            Выделенное и его обоснование — в карточке справа.
          </p>
        </Section>

        <Section
          step={4}
          title="Проверка и экспорт"
          status={
            validating ? (
              "проверка…"
            ) : violations === null ? undefined : violations.length === 0 ? (
              <span className="text-greenery-300">нарушений нет</span>
            ) : (
              <span className="text-red-300">нарушений {violations.length}</span>
            )
          }
          open={isOpen("export")}
          onToggle={() => toggle("export")}
          disabled={!hasPlan}
        >
          {violations !== null && violations.length > 0 && (
            <div className="rounded-lg border border-red-900 bg-red-950/50 p-2 text-xs text-red-300">
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
          <Button disabled={busy || exporting} onClick={() => guarded(() => onExportDxf())}>
            {exporting ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" aria-hidden /> : <Download className="mr-1.5 h-4 w-4" aria-hidden />}
            {exporting ? "Экспортируем…" : "Скачать DXF с подосновой"}
          </Button>
          <div>
            <p className="mb-1.5 text-xs text-stone-400">Обоснование каждой посадки (акт и пункт)</p>
            <div className="grid grid-cols-2 gap-1.5">
              <Button
                variant="secondary"
                size="sm"
                disabled={!onDownloadComplianceReport}
                onClick={() => onDownloadComplianceReport?.("json")}
                title="Обоснование каждой посадки со ссылкой на акт и пункт"
              >
                <FileJson className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                JSON
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={!onDownloadComplianceReport}
                onClick={() => onDownloadComplianceReport?.("csv")}
                title="То же построчно: посадка × норматив — для Excel"
              >
                <FileSpreadsheet className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                CSV для Excel
              </Button>
            </div>
          </div>
        </Section>

        <Section title="Слои и вид" open={isOpen("view")} onToggle={() => toggle("view")} disabled={!hasProject}>
          {layerLegend.length > 0 && (
            <ul className="flex flex-col gap-1">
              {layerLegend.map((entry) => (
                <li key={entry.key}>
                  <label className="flex items-center gap-2 text-sm text-stone-200">
                    <input
                      type="checkbox"
                      checked={!hiddenLayerTypes.has(entry.key)}
                      onChange={() => onToggleLayerType(entry.key)}
                      className="h-4 w-4 accent-greenery-600"
                    />
                    <span className="h-3 w-3 flex-none rounded-sm border border-white/10" style={{ backgroundColor: entry.color }} aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                    <span className="text-xs tabular-nums text-stone-500">{entry.count.toLocaleString("ru-RU")}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <Toggle checked={showPlan} onChange={onToggleShowPlan}>
            Сгенерированный план
          </Toggle>
          <Button
            variant={view3d ? "default" : "secondary"}
            size="sm"
            disabled={!view3d && !hasPlan}
            onClick={onToggleView3d}
            aria-pressed={view3d}
            title="Только просмотр — без выделения и правки"
          >
            <Box className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {view3d ? "Вернуться на 2D-карту" : "Показать в 3D"}
          </Button>
          {view3d && (
            <div className="grid grid-cols-4 gap-1">
              {(Object.keys(SEASON_LABELS) as Season[]).map((s) => (
                <button
                  key={s}
                  onClick={() => onSeasonChange(s)}
                  aria-pressed={season === s}
                  className={cn(
                    "rounded-md border px-1 py-1 text-xs",
                    season === s ? "border-greenery-600 bg-greenery-700/20 text-greenery-300" : "border-stone-700 text-stone-300 hover:bg-stone-800"
                  )}
                >
                  {SEASON_LABELS[s]}
                </button>
              ))}
            </div>
          )}
        </Section>
      </div>
    </aside>
  );
}

const BUDGET_TYPE_LABELS: Record<string, string> = { tree: "Деревья", shrub: "Кустарник", lawn: "Газон" };

/** Why a planting type got few (or no) plantings: the share of the
 * territory still open to it and the constraints that closed the rest. On a
 * dense street the strict setback tables leave trees almost nowhere to go --
 * without this, "only shrubs" reads as a generator bug instead of what the
 * norms actually say. */
function AreaBudgetList({ budgets }: { budgets: AreaBudget[] }) {
  const format = (m2: number) => Math.round(m2).toLocaleString("ru-RU");
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-stone-800 bg-stone-950/60 p-2.5 text-xs text-stone-300">
      <span className="text-stone-400">Сколько площади оставили нормы</span>
      {budgets.map((budget) => {
        const scarce = budget.usable_share < 0.05;
        return (
          <div key={budget.planting_type}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-medium text-stone-200">{BUDGET_TYPE_LABELS[budget.planting_type] ?? budget.planting_type}</span>
              <span className={cn("tabular-nums", scarce ? "text-amber-300" : "text-stone-400")}>
                {format(budget.usable_m2)} из {format(budget.territory_m2)} м² · {(100 * budget.usable_share).toFixed(1)}%
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-stone-800">
              <div
                className={cn("h-full rounded-full", scarce ? "bg-amber-400" : "bg-greenery-500")}
                style={{ width: `${Math.max(1, budget.usable_share * 100)}%`, backgroundColor: scarce ? undefined : PLANTING_COLORS[budget.planting_type] }}
              />
            </div>
            {scarce && budget.blockers.length > 0 && (
              <p className="mt-1 text-stone-500">
                Больше всего закрывают: {budget.blockers.slice(0, 3).map((b) => `${b.label} ${Math.round(100 * b.share)}%`).join(", ")}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}
