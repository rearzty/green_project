"use client";

import { useEffect, useState } from "react";
import { ChevronRight, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PanelTitle, ProgressBar, SectionLabel } from "@/components/panel/primitives";
import { estimatePlan, GenerationCancelledError, type EstimateItem, type PlantingType, type ScoringMode } from "@/lib/api";
import { pluralRu } from "@/lib/format";
import type { JobProgress } from "@/lib/hooks/useProjectSession";
import { PLANTING_COLORS } from "@/lib/mapStyle";
import { matchPreset, PLAN_PRESETS, type PresetSettings } from "@/lib/planPresets";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const ESTIMATE_FORMS: Record<"tree" | "shrub", [string, string, string]> = {
  tree: ["дерево", "дерева", "деревьев"],
  shrub: ["куст", "куста", "кустов"],
};

const nf = (n: number) => n.toLocaleString("ru-RU");

/** «55–65 деревьев», «≈150 кустов», «≈ 25 000 м²». По площади (плана ещё нет)
 * диапазон широкий и помечен «~»: цифры грубые, и сказано это прямо. */
function formatEstimate(e: EstimateItem): { value: string; noun: string } {
  const mid = (e.low + e.high) / 2;
  if (e.unit === "m2") return { value: `≈ ${nf(Math.round(mid / 100) * 100)}`, noun: "м² газона" };
  // Форма по большему числу диапазона: «5–7 кустов», «1–3 дерева».
  const noun = pluralRu(Math.round(e.high), ESTIMATE_FORMS[e.planting_type === "shrub" ? "shrub" : "tree"]);
  if (e.low === e.high) return { value: `≈ ${nf(e.low)}`, noun };
  return { value: `${e.basis === "area" ? "~" : ""}${nf(e.low)}–${nf(e.high)}`, noun };
}

/** Схема, чей прогноз по деревьям больше всего отличается от текущих настроек:
 * «Схема «Аллея» даст 100–120». Меньше десяти процентов — не повод говорить. */
interface PresetEstimate {
  title: string;
  low: number;
  high: number;
}

const GENERATE_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Деревья",
  shrub: "Кустарник",
  lawn: "Газон",
};

function Toggle({ checked, onChange, disabled, children, title }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; children: React.ReactNode; title?: string }) {
  return (
    <label className={cn("flex min-h-8 items-start gap-2.5 text-sm text-gp-text", disabled && "opacity-40")} title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="mt-0.5 h-4 w-4 flex-none accent-gp-accent" />
      <span>{children}</span>
    </label>
  );
}

export interface PlanPanelProps {
  hasProject: boolean;
  hasPlan: boolean;
  /** The delivery has no work-area boundary, only the survey order boundary,
   * and the user hasn't agreed to plan by it yet -- ask before generating. */
  needsOrderBoundaryConsent?: boolean;
  /** Показать тот же вопрос о границе заказа, что по кнопке генерации, —
   * когда план попросили построить не кнопкой, а Юне (app/page.tsx).
   * Каждое новое значение — новый вопрос; 0 — не спрашивали. */
  consentRequest?: number;
  generating?: boolean;
  generateProgress?: JobProgress | null;
  /** Сколько посадок каждого типа уже расставлено, пока план считается. */
  partialCounts?: Record<string, number>;
  /** «Остановить»: сервер отменяет задачу, план остаётся прежним. */
  onStopGenerate?: () => void;
  stopping?: boolean;
  /** Для прогноза «Ожидается»: проект и открытый план (от него масштабируется). */
  projectId?: string;
  planId?: string;
  editBusy: boolean;
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
}

/** Вкладка «План»: параметры новой версии и кнопка генерации. Прежний шаг 2
 * аккордеона ControlPanel с его логикой (guarded, вопрос о границе заказа
 * `consentRequest`). Каждая генерация — новая версия, прежние остаются в
 * «Версиях плана» на вкладке «Проверка». */
export function PlanPanel({
  hasProject,
  hasPlan,
  needsOrderBoundaryConsent = false,
  consentRequest = 0,
  generating = false,
  generateProgress = null,
  partialCounts,
  onStopGenerate,
  stopping = false,
  projectId,
  planId,
  editBusy,
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
}: PlanPanelProps) {
  const [askOrderBoundary, setAskOrderBoundary] = useState(false);
  const [busy, setBusy] = useState(false);

  // Согласие — только здесь, одним и тем же вопросом: Юна план по границе
  // заказа сама не строит, а открывает этот вопрос с её настройками в полях.
  useEffect(() => {
    if (consentRequest === 0 || !needsOrderBoundaryConsent) return;
    setAskOrderBoundary(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [consentRequest]);

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      // «Остановить» — не сбой: нейтральное уведомление вместо красного.
      if (e instanceof GenerationCancelledError) toast.info(e.message);
      else toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleType(type: PlantingType) {
    onPlantingTypesChange(plantingTypes.includes(type) ? plantingTypes.filter((t) => t !== type) : [...plantingTypes, type]);
  }

  const treesOn = plantingTypes.includes("tree");
  const shrubsOn = plantingTypes.includes("shrub");

  // Тонкая настройка свёрнута: частый путь — выбрать схему и сгенерировать.
  const [fineOpen, setFineOpen] = useState(false);
  // Выбранная схема не хранится, а выводится из полей (lib/planPresets.ts).
  const activePreset = matchPreset({ treeSpacingM, shrubSpacingM, treeRows, treesInPaving });
  function applyPreset(next: PresetSettings) {
    onTreeSpacingMChange(next.treeSpacingM);
    onShrubSpacingMChange(next.shrubSpacingM);
    onTreeRowsChange(next.treeRows);
    onTreesInPavingChange(next.treesInPaving);
  }

  // «Ожидается»: прогноз пересчитывается на каждую правку настроек, но не чаще
  // раза в 400 мс (debounce) и без устаревших ответов. Заодно — прогноз по
  // другим схемам, чтобы сказать, какая из них даст заметно другое число.
  const [estimate, setEstimate] = useState<EstimateItem[] | null>(null);
  const [betterPreset, setBetterPreset] = useState<PresetEstimate | null>(null);
  const typesKey = plantingTypes.join(",");
  useEffect(() => {
    if (!projectId || plantingTypes.length === 0) {
      setEstimate(null);
      setBetterPreset(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const settings = { plantingTypes, treeSpacingM, shrubSpacingM, treeRows, treesInPaving };
        const others = PLAN_PRESETS.filter((p) => p.key !== activePreset);
        const [own, ...rest] = await Promise.all([
          estimatePlan(projectId, settings, controller.signal),
          ...others.map((p) =>
            estimatePlan(projectId, { plantingTypes: ["tree"], ...p.settings }, controller.signal).catch(() => [] as EstimateItem[])
          ),
        ]);
        if (controller.signal.aborted) return;
        setEstimate(own);
        const ownTree = own.find((e) => e.planting_type === "tree");
        let best: PresetEstimate | null = null;
        if (ownTree && plantingTypes.includes("tree")) {
          const ownMid = (ownTree.low + ownTree.high) / 2;
          others.forEach((preset, i) => {
            const tree = rest[i]?.find((e) => e.planting_type === "tree");
            if (!tree || ownMid <= 0) return;
            const diff = Math.abs((tree.low + tree.high) / 2 - ownMid) / ownMid;
            if (diff >= 0.1 && (!best || diff > Math.abs(((best.low + best.high) / 2 - ownMid) / ownMid))) {
              best = { title: preset.title, low: tree.low, high: tree.high };
            }
          });
        }
        setBetterPreset(best);
      } catch {
        // Прогноз — подсказка: нет ответа — блока просто нет, генерации это не мешает.
        if (!controller.signal.aborted) {
          setEstimate(null);
          setBetterPreset(null);
        }
      }
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
    // plantingTypes — по typesKey (массив новый на каждый рендер страницы)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, planId, typesKey, treeSpacingM, shrubSpacingM, treeRows, treesInPaving]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <PanelTitle title="Новая версия плана" hint="Отступы по нормам соблюдаются всегда" />

        <section aria-labelledby="plan-what" className="px-5 pb-4 pt-4">
          <SectionLabel id="plan-what">Что сажать</SectionLabel>
          <div className="mt-2 grid grid-cols-3 gap-2">
            {(Object.keys(GENERATE_TYPE_LABELS) as PlantingType[]).map((type) => {
              const on = plantingTypes.includes(type);
              return (
                <button
                  key={type}
                  onClick={() => toggleType(type)}
                  aria-pressed={on}
                  disabled={!hasProject}
                  className={cn(
                    "flex h-10 items-center justify-center gap-2 rounded-lg border px-2 text-[13px] font-medium transition-colors disabled:opacity-40",
                    on ? "border-gp-accent-soft-border bg-gp-accent-soft text-gp-text" : "border-gp-border text-gp-text-2 hover:bg-gp-hover"
                  )}
                >
                  <span className="h-2.5 w-2.5 flex-none rounded-full" style={{ backgroundColor: on ? PLANTING_COLORS[type] : "#5C645F" }} aria-hidden />
                  {GENERATE_TYPE_LABELS[type]}
                </button>
              );
            })}
          </div>
        </section>

        <section aria-labelledby="plan-scheme" className="border-t border-gp-line px-5 pb-3 pt-4">
          <SectionLabel id="plan-scheme">Схема посадки</SectionLabel>
          <div role="radiogroup" aria-labelledby="plan-scheme" className="-mx-3 mt-2 flex flex-col gap-0.5">
            {PLAN_PRESETS.map((preset) => {
              const on = activePreset === preset.key;
              return (
                <button
                  key={preset.key}
                  role="radio"
                  aria-checked={on}
                  disabled={!hasProject}
                  onClick={() => applyPreset(preset.settings)}
                  className={cn("flex w-full gap-3 rounded-lg p-3 text-left transition-colors hover:bg-gp-hover disabled:opacity-40", on && "bg-gp-hover")}
                >
                  <span className={cn("mt-0.5 h-4 w-4 flex-none rounded-full border-solid", on ? "border-[5px] border-gp-accent" : "border-[1.5px] border-gp-text-4")} aria-hidden />
                  <span>
                    <span className="block text-sm font-medium text-gp-text">{preset.title}</span>
                    <span className="mt-0.5 block text-[13px] leading-5 text-gp-text-2">{preset.note}</span>
                  </span>
                </button>
              );
            })}
          </div>
          {activePreset === null && <p className="mt-1 text-[13px] leading-5 text-gp-text-3">Свои настройки — они в «Тонкой настройке» ниже.</p>}
          <button
            onClick={() => setFineOpen((v) => !v)}
            aria-expanded={fineOpen}
            className="mt-1 flex min-h-9 w-full items-center gap-1.5 text-left text-[13px] text-gp-text-2 hover:text-gp-text"
          >
            <ChevronRight className={cn("h-4 w-4 transition-transform", fineOpen && "rotate-90")} aria-hidden />
            Тонкая настройка: интервалы, способ выбора мест
          </button>
        </section>

        {fineOpen && (
          <>
            <section aria-labelledby="plan-spacing" className="border-t border-gp-line px-5 pb-4 pt-4">
              <SectionLabel id="plan-spacing">Интервалы, м</SectionLabel>
              <div className="mt-2 grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1.5 text-[13px] text-gp-text-3">
                  Деревья
                  <input
                    type="number"
                    step={0.5}
                    min={0.5}
                    max={15}
                    value={treeSpacingM ?? ""}
                    onChange={(e) => onTreeSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
                    disabled={!treesOn}
                    // По норме интервал задаёт класс кроны породы, а не одно число;
                    // своё значение только раздвигает деревья (geo_engine/planner.py).
                    placeholder="3–8 (по кроне)"
                    title="По норме — по классу кроны: 8 м широкая, 5 м средняя, 3 м узкая (МГСН 1.02-02 п. 4.2.9.2). Своё значение может только раздвинуть деревья, ближе нормы их не поставить."
                    className="h-10 rounded-lg border border-gp-border bg-gp-field px-3 font-mono text-sm text-gp-text placeholder:font-sans placeholder:text-gp-text-4 disabled:opacity-40"
                  />
                </label>
                <label className="flex flex-col gap-1.5 text-[13px] text-gp-text-3">
                  Кустарник
                  <input
                    type="number"
                    step={0.1}
                    min={0.3}
                    max={10}
                    value={shrubSpacingM ?? ""}
                    onChange={(e) => onShrubSpacingMChange(e.target.value === "" ? undefined : Number(e.target.value))}
                    disabled={!shrubsOn}
                    // Почти весь кустарник — живая изгородь, и интервал пользователя
                    // заменяет именно её шаг (planner.IN_ROW_PITCH_M), а не 3 м
                    // из planting_norms.yaml.
                    placeholder="0,5 (изгородь)"
                    title="Шаг кустарника в живой изгороди: по норме 0,5 м (743-ПП табл. 3.6.2). Куртины сажаются по плотности породы (515-ПП табл. 5)."
                    className="h-10 rounded-lg border border-gp-border bg-gp-field px-3 font-mono text-sm text-gp-text placeholder:font-sans placeholder:text-gp-text-4 disabled:opacity-40"
                  />
                </label>
              </div>
              <div className="mt-3 flex flex-col gap-1">
                <Toggle
                  checked={treeRows === 2}
                  onChange={(v) => onTreeRowsChange(v ? 2 : 1)}
                  disabled={!treesOn}
                  title="743-ПП табл. 3.6.2: двухрядная посадка, интервал 7–8 м, второй ряд в шахматном порядке"
                >
                  Двухрядная аллея вдоль улиц
                </Toggle>
                <Toggle
                  checked={treesInPaving}
                  onChange={onTreesInPavingChange}
                  disabled={!treesOn}
                  title="743-ПП п. 9.8: околоствольное пространство Ø 2 м с приствольной решёткой; остальные отступы не меняются"
                >
                  Деревья в покрытии тротуара <span className="text-gp-text-3">(решётка Ø 2 м)</span>
                </Toggle>
              </div>
            </section>

            <section aria-labelledby="plan-scoring" className="border-t border-gp-line px-5 pb-5 pt-4">
              <SectionLabel id="plan-scoring">Выбор лучших мест</SectionLabel>
              <div role="group" aria-labelledby="plan-scoring" className="mt-2 grid grid-cols-2 rounded-lg border border-gp-border p-0.5 text-[13px]">
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
                    className={cn("h-8 rounded-md px-2 font-medium transition-colors", scoringMode === mode ? "bg-gp-border text-gp-text" : "text-gp-text-2 hover:text-gp-text")}
                  >
                    {mode === "heuristic" ? "Формула" : "ML-модель"}
                  </button>
                ))}
              </div>
            </section>
          </>
        )}

        {estimate && estimate.length > 0 && (
          <section aria-labelledby="plan-estimate" className="border-t border-gp-line px-5 pb-5 pt-4">
            <SectionLabel id="plan-estimate">Ожидается</SectionLabel>
            <div className="mt-2 flex flex-col gap-1.5">
              {estimate.map((item) => {
                const text = formatEstimate(item);
                return (
                  <p key={item.planting_type} className="m-0 flex items-baseline gap-2">
                    <span className="font-mono text-[17px] tabular-nums text-gp-text">{text.value}</span>
                    <span className="text-sm text-gp-text-2">{text.noun}</span>
                  </p>
                );
              })}
            </div>
            <p className="mt-2 text-[13px] leading-5 text-gp-text-3">
              {estimate.some((e) => e.basis === "area")
                ? "Грубая оценка по свободной площади — после первой генерации прогноз точнее."
                : "Оценка от открытой версии плана: меняется только то, что вы поменяли в настройках."}
              {betterPreset && (
                <>
                  {" "}
                  Схема «{betterPreset.title}» даст{" "}
                  <span className="font-mono tabular-nums text-gp-text-2">
                    {betterPreset.low === betterPreset.high ? `≈ ${nf(betterPreset.low)}` : `${nf(betterPreset.low)}–${nf(betterPreset.high)}`}
                  </span>{" "}
                  деревьев.
                </>
              )}
            </p>
          </section>
        )}
      </div>

      <div className="flex-none border-t border-gp-line px-5 pb-5 pt-4">
        {askOrderBoundary && needsOrderBoundaryConsent && (
          <div className="mb-4 flex flex-col gap-2 rounded-lg border border-gp-warn-bar/50 bg-gp-warn-bar/10 p-3 text-[13px] leading-5 text-gp-warn">
            <p className="font-medium">Границы работ не найдены — генерировать по границе заказа на съёмку?</p>
            <p className="text-gp-warn/80">
              Граница заказа шире фактического участка работ: отступы от сетей и зданий соблюдаются, но посадки могут оказаться за участком.
              Результат предварительный.
            </p>
            <div className="mt-1 flex gap-2">
              <Button
                size="sm"
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
        {generating && (
          <div className="mb-4 flex flex-col gap-3">
            {generateProgress ? <ProgressBar label={generateProgress.stage} progress={generateProgress.progress} /> : <ProgressBar label="Считаю…" progress={null} />}
            {partialCounts && Object.keys(partialCounts).length > 0 && (
              <p className="m-0 text-[13px] leading-5 text-gp-text-2">
                Уже расставлено{" "}
                {(["tree", "shrub"] as const)
                  .filter((type) => partialCounts[type])
                  .map((type) => `${nf(partialCounts[type])} ${pluralRu(partialCounts[type], ESTIMATE_FORMS[type])}`)
                  .join(", ")}
                {partialCounts.lawn ? `${partialCounts.tree || partialCounts.shrub ? ", " : " "}газон` : ""} — план появляется по мере расчёта.
              </p>
            )}
            {onStopGenerate && (
              <Button variant="outline" size="sm" className="self-start" disabled={stopping} onClick={onStopGenerate}>
                {stopping ? "Останавливаю…" : "Остановить"}
              </Button>
            )}
          </div>
        )}
        <Button
          size="lg"
          className="w-full"
          disabled={!hasProject || plantingTypes.length === 0 || busy || editBusy || generating}
          onClick={() =>
            needsOrderBoundaryConsent
              ? setAskOrderBoundary(true)
              : guarded(() => onGenerate(plantingTypes, scoringMode, { treeSpacingM, shrubSpacingM, treeRows, treesInPaving }))
          }
        >
          {generating && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {generating ? "Генерация…" : hasPlan ? "Сгенерировать заново" : "Сгенерировать план"}
        </Button>
      </div>
    </div>
  );
}
