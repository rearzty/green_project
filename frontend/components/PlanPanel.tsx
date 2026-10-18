"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PanelTitle, ProgressBar, SectionLabel } from "@/components/panel/primitives";
import type { PlantingType, ScoringMode } from "@/lib/api";
import type { JobProgress } from "@/lib/hooks/useProjectSession";
import { PLANTING_COLORS } from "@/lib/mapStyle";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

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
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleType(type: PlantingType) {
    onPlantingTypesChange(plantingTypes.includes(type) ? plantingTypes.filter((t) => t !== type) : [...plantingTypes, type]);
  }

  const treesOn = plantingTypes.includes("tree");
  const shrubsOn = plantingTypes.includes("shrub");

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
        {generateProgress && (
          <div className="mb-4">
            <ProgressBar label={generateProgress.stage} progress={generateProgress.progress} />
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
