import { CheckCircle2, Loader2, Shrub, Sprout, TreeDeciduous, TriangleAlert } from "lucide-react";

import type { PlanStats } from "@/lib/planStats";
import { PLANTING_COLORS } from "@/lib/mapStyle";
import { cn } from "@/lib/utils";

const format = (n: number) => Math.round(n).toLocaleString("ru-RU");

/** Итог текущего плана поверх карты: сколько посажено и прошёл ли он
 * проверку нормативов. Заменил «индекс озеленения» -- тот был средним
 * баллом скоринга, который не объяснить словами. */
export function PlanSummaryCard({
  stats,
  violations,
  validating,
  territoryM2,
  scoringMode,
}: {
  stats: PlanStats;
  /** null -- план ещё не проверен. */
  violations: number | null;
  validating: boolean;
  /** Площадь участка, м² (PlanOut.area_budget[].territory_m2). */
  territoryM2?: number;
  scoringMode?: "heuristic" | "ml";
}) {
  // Покрытие кронами (tree canopy cover) — стандартный показатель городского
  // озеленения. Крона может нависать над тротуаром или отступом, поэтому
  // делим на весь участок, а не на площадь, где разрешено стоять стволу.
  const canopyShare = territoryM2 && territoryM2 > 0 ? Math.min(1, stats.canopyM2 / territoryM2) : null;
  const percent = (v: number) => `${Math.round(v * 100)}%`;
  const rows = [
    { key: "tree", icon: TreeDeciduous, label: "Деревья", value: format(stats.trees), unit: "шт." },
    { key: "shrub", icon: Shrub, label: "Кустарник", value: format(stats.shrubs), unit: "шт." },
    { key: "lawn", icon: Sprout, label: "Газон", value: format(stats.lawnAreaM2), unit: "м²" },
  ];

  return (
    <div className="pointer-events-auto w-72 rounded-xl border border-stone-700/80 bg-stone-900/90 p-3 text-stone-100 shadow-lg backdrop-blur">
      <div className="mb-2 flex items-baseline justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-stone-400">План озеленения</p>
        {scoringMode && (
          <span
            className="rounded-full bg-stone-800 px-2 py-0.5 text-[10px] text-stone-300"
            title={scoringMode === "ml" ? "Места деревьев отобраны моделью по проектным решениям пилота" : "Места выбраны формулой"}
          >
            {scoringMode === "ml" ? "ML-модель" : "эвристика"}
          </span>
        )}
      </div>
      <div className="grid grid-cols-3 gap-2">
        {rows.map(({ key, icon: Icon, label, value, unit }) => (
          <div key={key} className="rounded-lg bg-stone-800/70 px-2 py-1.5">
            <div className="flex items-center gap-1 text-[11px] text-stone-400">
              <Icon className="h-3.5 w-3.5" style={{ color: PLANTING_COLORS[key] }} aria-hidden />
              {label}
            </div>
            <p className="mt-0.5 text-base font-semibold leading-tight tabular-nums">
              {value}
              <span className="ml-0.5 text-[11px] font-normal text-stone-400">{unit}</span>
            </p>
          </div>
        ))}
      </div>
      {stats.trees > 0 && (
        <dl className="mt-2 grid grid-cols-[1fr_auto] gap-x-2 gap-y-0.5 text-[11px]">
          {canopyShare !== null && (
            <>
              <dt className="text-stone-400" title="Сумма площадей крон деревьев (по диаметру кроны породы) к площади участка — оценка сверху, без вычета перекрытий">
                Покрытие участка кронами
              </dt>
              <dd className="text-right tabular-nums text-stone-200">{percent(canopyShare)}</dd>
            </>
          )}
          {stats.designedShare !== null && (
            <>
              <dt className="text-stone-400" title="Ряды вдоль улиц, группы и солитеры — схемы 743-ПП, а не россыпь">
                Деревьев по схеме (ряд, группа, солитер)
              </dt>
              <dd className="text-right tabular-nums text-stone-200">{percent(stats.designedShare)}</dd>
            </>
          )}
          {stats.topSpeciesShare !== null && (
            <>
              <dt className="text-stone-400">Пород: {stats.speciesCount}, самая частая</dt>
              <dd className="text-right tabular-nums text-stone-200">{percent(stats.topSpeciesShare)}</dd>
            </>
          )}
        </dl>
      )}
      <div
        className={cn(
          "mt-2 flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs",
          violations === null || validating
            ? "bg-stone-800/70 text-stone-400"
            : violations === 0
              ? "bg-greenery-700/25 text-greenery-300"
              : "bg-red-950/70 text-red-300"
        )}
      >
        {validating ? (
          <>
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Проверяю нормативы…
          </>
        ) : violations === null ? (
          <>Нормативы ещё не проверены</>
        ) : violations === 0 ? (
          <>
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
            Все нормативные отступы соблюдены
          </>
        ) : (
          <>
            <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
            Нарушений норматива: {violations} — отмечены красным
          </>
        )}
      </div>
    </div>
  );
}
