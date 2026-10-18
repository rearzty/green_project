import { useState } from "react";
import { CheckCircle2, ChevronDown, Loader2, Shrub, Sprout, Trash2, TreeDeciduous, TriangleAlert, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import type { ComplianceCheck, ItemCompliance, PlantingType } from "@/lib/api";
import { countLabel, OBJECT_FORMS, PLANTING_TYPE_FORMS, PLANTING_TYPE_LABELS } from "@/lib/format";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import { PLANTING_COLORS } from "@/lib/mapStyle";
import { cn } from "@/lib/utils";

const TYPE_ICONS = { tree: TreeDeciduous, shrub: Shrub, lawn: Sprout } as const;

const CROWN_LABELS: Record<string, string> = { wide: "широкая крона", medium: "средняя крона", narrow: "узкая крона" };

/** Properties of the one selected plan feature (geo_io.py) the card shows
 * beyond what the compliance endpoint returns. */
export interface InspectedItem {
  species?: string;
  crown?: string | null;
  score?: number;
  rationale?: string;
  is_manual_edit?: boolean;
}

const meters = (m: number) => `${m.toLocaleString("ru-RU", { maximumFractionDigits: m < 10 ? 2 : 1 })} м`;

/** Одна строка таблицы отступов: что, сколько требует норма, сколько
 * фактически, и полоска запаса -- заполнена настолько, насколько
 * фактическое расстояние покрывает требуемое (до двукратного запаса). */
function CheckRow({ check, binding }: { check: ComplianceCheck; binding: boolean }) {
  const ratio = check.required_m > 0 ? check.actual_m / check.required_m : 2;
  const fill = Math.min(1, ratio / 2);
  return (
    <li className={cn("rounded-md px-2 py-1.5", binding ? "bg-stone-800" : "", !check.satisfied && "bg-red-950/60")}>
      {binding && <p className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-greenery-300">Решающее ограничение</p>}
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className={cn("min-w-0 truncate", check.satisfied ? "text-stone-200" : "text-red-300")} title={check.object_label}>
          {check.object_label || check.object_type}
        </span>
        <span className="flex-none tabular-nums text-stone-400">
          <span className="text-stone-500">норма</span> {meters(check.required_m)} ·{" "}
          <span className={check.satisfied ? "text-stone-100" : "font-semibold text-red-300"}>{meters(check.actual_m)}</span>
        </span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-stone-700">
        <div
          className={cn("h-full rounded-full", !check.satisfied ? "bg-red-500" : ratio < 1.25 ? "bg-amber-400" : "bg-greenery-500")}
          style={{ width: `${Math.max(4, fill * 100)}%` }}
        />
      </div>
      <p className="mt-1 text-[10.5px] leading-snug text-stone-500" title={check.table_row}>
        {check.citation}
        {!check.verified && <span className="ml-1 rounded bg-amber-900/50 px-1 text-amber-300">не сверено с текстом акта</span>}
      </p>
    </li>
  );
}

/** «Почему здесь?» для одной посадки -- обоснование по НПА (требование ТЗ:
 * ссылка на акт и пункт для каждой позиции) таблицей, а не сплошным
 * текстом. Для группы -- состав выделения и действия над ним. */
export function InspectorCard({
  selection,
  item,
  compliance,
  complianceLoading,
  canEdit,
  onRetype,
  onDelete,
  onClear,
}: {
  selection: SelectionSummary;
  item?: InspectedItem;
  compliance: ItemCompliance | null;
  complianceLoading: boolean;
  canEdit: boolean;
  onRetype: (type: "tree" | "shrub") => void;
  onDelete: () => void;
  onClear: () => void;
}) {
  const [showRationale, setShowRationale] = useState(false);
  const single = selection.single;
  const Icon = single ? TYPE_ICONS[single.type] : null;
  const checks = compliance ? [...compliance.checks].sort((a, b) => a.actual_m - a.required_m - (b.actual_m - b.required_m)) : [];
  const violation = single?.violation || (compliance !== null && !compliance.compliant);
  // Поправка по породе одна на посадку (крона больше 5 м -- все табличные
  // отступы ×k), повторять её в каждой строке незачем.
  const speciesRule = checks.find((c) => c.species_rule)?.species_rule;

  return (
    // data-map-overlay: лежит поверх карты — MapView.tsx::freeMapRects не
    // кладёт под карточку рамку «выдели …».
    <div
      data-map-overlay
      className="pointer-events-auto flex max-h-full w-72 flex-col overflow-hidden rounded-xl border border-stone-700/80 bg-stone-900/95 text-stone-100 shadow-lg backdrop-blur"
    >
      <div className="flex items-start justify-between gap-2 border-b border-stone-800 p-3">
        <div className="flex min-w-0 items-center gap-2">
          {Icon && single ? (
            <span className="flex h-8 w-8 flex-none items-center justify-center rounded-lg" style={{ backgroundColor: `${PLANTING_COLORS[single.type]}33` }}>
              <Icon className="h-4 w-4" style={{ color: PLANTING_COLORS[single.type] }} aria-hidden />
            </span>
          ) : null}
          <div className="min-w-0">
            {single ? (
              <>
                <p className="truncate text-sm font-semibold">{item?.species && item.species !== "default" ? item.species : PLANTING_TYPE_LABELS[single.type]}</p>
                <p className="truncate text-xs text-stone-400">
                  {PLANTING_TYPE_LABELS[single.type]}
                  {item?.crown && CROWN_LABELS[item.crown] ? ` · ${CROWN_LABELS[item.crown]}` : ""}
                  {item?.is_manual_edit ? " · изменено вручную" : ""}
                </p>
              </>
            ) : (
              <>
                <p className="text-sm font-semibold">Выделено: {countLabel(selection.total, OBJECT_FORMS)}</p>
                <p className="truncate text-xs text-stone-400">
                  {(Object.keys(selection.counts) as PlantingType[])
                    .filter((type) => selection.counts[type] > 0)
                    .map((type) => countLabel(selection.counts[type], PLANTING_TYPE_FORMS[type]))
                    .join(", ")}
                </p>
              </>
            )}
          </div>
        </div>
        <button onClick={onClear} className="flex flex-none items-center gap-1 rounded p-1 text-stone-400 hover:bg-stone-800 hover:text-stone-200" title="Снять выделение (Esc)">
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="flex gap-1.5 border-b border-stone-800 px-3 py-2">
        <Button variant="secondary" size="sm" className="flex-1 gap-1.5 px-2" disabled={!canEdit} onClick={() => onRetype("tree")} title="Сделать деревом (1)">
          <TreeDeciduous className="h-3.5 w-3.5" aria-hidden />
          Дерево <Kbd>1</Kbd>
        </Button>
        <Button variant="secondary" size="sm" className="flex-1 gap-1.5 px-2" disabled={!canEdit} onClick={() => onRetype("shrub")} title="Сделать кустарником (2)">
          <Shrub className="h-3.5 w-3.5" aria-hidden />
          Куст <Kbd>2</Kbd>
        </Button>
        <Button
          variant="secondary"
          size="icon"
          className="flex-none border-red-900/70 text-red-300 hover:border-red-800 hover:bg-red-950/60"
          disabled={!canEdit}
          onClick={onDelete}
          title="Удалить (Del)"
          aria-label="Удалить"
        >
          <Trash2 className="h-3.5 w-3.5" aria-hidden />
        </Button>
      </div>

      {single && (
        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          <div
            className={cn(
              "mb-2 flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium",
              complianceLoading && !compliance ? "bg-stone-800 text-stone-400" : violation ? "bg-red-950/70 text-red-300" : "bg-greenery-700/25 text-greenery-300"
            )}
          >
            {complianceLoading && !compliance ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                Считаю обоснование…
              </>
            ) : violation ? (
              <>
                <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
                Нарушен нормативный отступ
              </>
            ) : (
              <>
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                Посадка допустима по нормам
              </>
            )}
          </div>

          {compliance && (
            <>
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-stone-500">
                {violation ? "Нормативные отступы" : "Почему здесь можно"}
              </p>
              {speciesRule && <p className="mb-1.5 text-[11px] leading-snug text-stone-400">Поправка по породе: {speciesRule}</p>}
              {checks.length === 0 ? (
                <p className="text-xs text-stone-400">Нормируемых препятствий рядом нет.</p>
              ) : (
                <ul className="flex flex-col gap-1">
                  {checks.map((check) => (
                    <CheckRow key={check.object_type} check={check} binding={check.object_type === compliance.binding_constraint} />
                  ))}
                </ul>
              )}
            </>
          )}
          {!complianceLoading && !compliance && <p className="text-xs text-stone-500">Обоснование недоступно.</p>}

          {item?.rationale && (
            <div className="mt-2 border-t border-stone-800 pt-2">
              <button
                onClick={() => setShowRationale((v) => !v)}
                className="flex w-full items-center justify-between text-[11px] font-medium uppercase tracking-wide text-stone-500 hover:text-stone-300"
              >
                Почему выбрано это место
                <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", showRationale && "rotate-180")} aria-hidden />
              </button>
              {showRationale && <p className="mt-1 text-xs leading-relaxed text-stone-300">{item.rationale}</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
