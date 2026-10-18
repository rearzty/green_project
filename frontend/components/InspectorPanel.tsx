import { useState } from "react";
import { Check, ChevronDown, Loader2, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { RightColumn } from "@/components/RightColumn";
import type { ComplianceCheck, ItemCompliance, PlantingType } from "@/lib/api";
import { countLabel, CROWN_LABELS, formatDistancePair, formatMeters, OBJECT_FORMS, PLANTING_TYPE_FORMS, PLANTING_TYPE_LABELS } from "@/lib/format";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import { cn } from "@/lib/utils";

/** Сколько самых критичных отступов видно сразу: остальные — по раскрытию. */
const TOP_CHECKS = 3;
/** Запас меньше 25% от нормы — полоса жёлтая (`gp-warn`), как в задании. */
const WARN_RATIO = 1.25;

/** Properties of the one selected plan feature (geo_io.py) the panel shows
 * beyond what the compliance endpoint returns. */
export interface InspectedItem {
  species?: string;
  crown?: string | null;
  score?: number;
  rationale?: string;
  is_manual_edit?: boolean;
}

/** Одна строка отступов: название, «**4,6** из 4 м» и полоса запаса. Полоса
 * заполнена настолько, насколько факт покрывает норму до двукратного запаса
 * (заполнение = факт / (2 × норма)): нарушение — `gp-violation`, запас
 * меньше 25% — `gp-warn`, иначе `gp-ok`. */
function CheckBar({ check, note, expanded }: { check: ComplianceCheck; note?: string; expanded: boolean }) {
  const ratio = check.required_m > 0 ? check.actual_m / check.required_m : 2;
  const fill = Math.min(1, ratio / 2);
  const pair = formatDistancePair(check.actual_m, check.required_m);
  return (
    <li className="py-2">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className={cn("min-w-0 truncate first-letter:uppercase", check.satisfied ? "text-gp-text" : "text-gp-violation-text")} title={check.object_label}>
          {check.object_label || check.object_type}
        </span>
        <span className="flex-none font-mono text-[13px] tabular-nums text-gp-text-2">
          <span className={cn("font-medium", check.satisfied ? "text-gp-text" : "text-gp-violation-text")}>{pair.actual.replace(" м", "")}</span> из {pair.required}
        </span>
      </div>
      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-gp-line" role="img" aria-label={`Запас: ${formatMeters(check.actual_m - check.required_m, 2)}`}>
        <div
          className={cn("h-full rounded-full", !check.satisfied ? "bg-gp-violation" : ratio < WARN_RATIO ? "bg-gp-warn-bar" : "bg-gp-ok-strong")}
          style={{ width: `${Math.max(4, fill * 100)}%` }}
        />
      </div>
      {note && <p className="mt-1 text-xs text-gp-text-3">{note}</p>}
      {expanded && (
        <p className="mt-1 text-xs leading-5 text-gp-text-3" title={check.table_row}>
          {check.citation}
          {!check.verified && (
            <span className="ml-1.5 rounded bg-gp-warn-bar/20 px-1.5 py-px text-gp-warn" title="Число взято не из текста акта — сверьте с документом">
              не сверено с актом
            </span>
          )}
        </p>
      )}
    </li>
  );
}

/** Правая колонка при выделении: для одной посадки — «Почему здесь?»,
 * обоснование по НПА (требование ТЗ: ссылка на акт и пункт для каждой
 * позиции); для группы — состав выделения и действия над ним. Заменила
 * плавающую InspectorCard над картой (Yuna.dc.html — образец колонки). */
export function InspectorPanel({
  selection,
  item,
  compliance,
  complianceLoading,
  canEdit,
  onRetype,
  onDelete,
  onClear,
  onAskYuna,
}: {
  selection: SelectionSummary;
  item?: InspectedItem;
  compliance: ItemCompliance | null;
  complianceLoading: boolean;
  canEdit: boolean;
  onRetype: (type: "tree" | "shrub") => void;
  onDelete: () => void;
  onClear: () => void;
  /** «Спросить Юну об этой посадке» — открывает чат с контекстом выделения. */
  onAskYuna: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const single = selection.single;
  const checks = compliance ? [...compliance.checks].sort((a, b) => a.actual_m - a.required_m - (b.actual_m - b.required_m)) : [];
  const top = showAll ? checks : checks.slice(0, TOP_CHECKS);
  const hidden = checks.length - TOP_CHECKS;
  const violation = single?.violation || (compliance !== null && !compliance.compliant);
  const failed = checks.filter((c) => !c.satisfied);
  // Поправка по породе одна на посадку (крона больше 5 м -- все табличные
  // отступы ×k), повторять её в каждой строке незачем.
  const speciesRule = checks.find((c) => c.species_rule)?.species_rule;
  const tightest = checks[0];
  const tightestNote = tightest
    ? tightest.satisfied
      ? `Запас всего ${formatMeters(tightest.actual_m - tightest.required_m, 2)} — ближе всего к пределу`
      : `Не хватает ${formatMeters(tightest.required_m - tightest.actual_m, 2)}`
    : undefined;
  const speciesName = item?.species && item.species !== "default" ? item.species : single ? PLANTING_TYPE_LABELS[single.type] : "";

  return (
    <RightColumn label="Выделение">
      <div className="flex items-start gap-2 border-b border-gp-line py-4 pl-5 pr-3">
        <div className="min-w-0 flex-1">
          {single ? (
            <>
              <h2 className="truncate text-xl font-semibold leading-7 text-gp-text">{speciesName}</h2>
              <p className="mt-0.5 truncate text-[13px] text-gp-text-2">
                {PLANTING_TYPE_LABELS[single.type]}
                {item?.crown && CROWN_LABELS[item.crown] ? ` · ${CROWN_LABELS[item.crown]}` : ""}
                {item?.is_manual_edit ? " · изменено вручную" : ""}
              </p>
            </>
          ) : (
            <>
              <h2 className="text-xl font-semibold leading-7 text-gp-text">Выделено: {countLabel(selection.total, OBJECT_FORMS)}</h2>
              <p className="mt-0.5 truncate text-[13px] text-gp-text-2">
                {(Object.keys(selection.counts) as PlantingType[])
                  .filter((type) => selection.counts[type] > 0)
                  .map((type) => countLabel(selection.counts[type], PLANTING_TYPE_FORMS[type]))
                  .join(", ")}
              </p>
            </>
          )}
        </div>
        <button
          onClick={onClear}
          className="flex h-9 w-9 flex-none items-center justify-center rounded-md text-gp-text-2 hover:bg-gp-hover hover:text-gp-text"
          title="Снять выделение · Esc"
          aria-label="Снять выделение"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="flex items-center gap-2 border-b border-gp-line px-5 py-3">
        <div role="group" aria-label="Тип посадки" className="grid flex-1 grid-cols-2 rounded-lg border border-gp-border p-0.5 text-[13px]">
          {(["tree", "shrub"] as const).map((type, i) => (
            <button
              key={type}
              disabled={!canEdit}
              onClick={() => onRetype(type)}
              aria-pressed={single?.type === type}
              title={`${type === "tree" ? "Сделать деревом" : "Сделать кустарником"} · ${i + 1}`}
              className={cn(
                "flex h-8 items-center justify-center gap-1.5 rounded-md px-2 font-medium transition-colors disabled:opacity-40",
                single?.type === type ? "bg-gp-border text-gp-text" : "text-gp-text-2 hover:text-gp-text"
              )}
            >
              {type === "tree" ? "Дерево" : "Куст"} <Kbd>{i + 1}</Kbd>
            </button>
          ))}
        </div>
        <Button
          variant="outline"
          size="sm"
          className="h-9 border-gp-violation-soft px-3 text-gp-violation-danger hover:bg-gp-violation-soft/50"
          disabled={!canEdit}
          onClick={onDelete}
          title="Удалить · Del"
          aria-label="Удалить"
        >
          <Trash2 className="h-4 w-4" aria-hidden />
          <Kbd className="text-gp-violation-danger/70">Del</Kbd>
        </Button>
      </div>

      {single ? (
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <p
            className={cn(
              "flex items-center gap-2 text-sm font-medium",
              complianceLoading && !compliance ? "text-gp-text-2" : violation ? "text-gp-violation-text" : "text-gp-ok"
            )}
          >
            {complianceLoading && !compliance ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                Считаю обоснование…
              </>
            ) : violation ? (
              <>
                <span className="h-2 w-2 flex-none rounded-full bg-gp-violation" aria-hidden />
                {failed.length > 0 ? `Нарушен отступ: ${failed.map((c) => c.object_label).join(", ")}` : "Нарушен нормативный отступ"}
              </>
            ) : (
              <>
                <Check className="h-4 w-4 flex-none" strokeWidth={2.2} aria-hidden />
                Все отступы соблюдены
              </>
            )}
          </p>

          {compliance && (
            <section aria-labelledby="inspector-checks" className="mt-4">
              <h3 id="inspector-checks" className="text-[13px] font-medium text-gp-text-2">
                Отступы
              </h3>
              {speciesRule && <p className="mt-1 text-xs leading-5 text-gp-text-3">Поправка по породе: {speciesRule}</p>}
              {checks.length === 0 ? (
                <p className="mt-2 text-sm text-gp-text-3">Нормируемых препятствий рядом нет.</p>
              ) : (
                <ul className="mt-1 divide-y divide-gp-line">
                  {top.map((check, i) => (
                    <CheckBar key={check.object_type} check={check} note={i === 0 ? tightestNote : undefined} expanded={showAll} />
                  ))}
                </ul>
              )}
              {hidden > 0 && (
                <button
                  onClick={() => setShowAll((v) => !v)}
                  aria-expanded={showAll}
                  className="mt-1 flex h-9 items-center gap-1.5 text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover"
                >
                  {showAll ? "Свернуть" : `Ещё ${hidden} ${hidden === 1 ? "отступ" : hidden < 5 ? "отступа" : "отступов"} и ссылки на нормы`}
                  <ChevronDown className={cn("h-4 w-4 transition-transform", showAll && "rotate-180")} aria-hidden />
                </button>
              )}
              {hidden <= 0 && checks.length > 0 && !showAll && (
                <button onClick={() => setShowAll(true)} className="mt-1 flex h-9 items-center text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover">
                  Ссылки на нормы
                </button>
              )}
            </section>
          )}
          {!complianceLoading && !compliance && <p className="mt-3 text-sm text-gp-text-3">Обоснование недоступно.</p>}

          {item?.rationale && (
            <section aria-labelledby="inspector-why" className="mt-4">
              <h3 id="inspector-why" className="mb-1.5 text-[13px] font-medium text-gp-text-2">
                Почему здесь
              </h3>
              <p className="rounded-lg bg-gp-hover px-3 py-2.5 text-sm leading-6 text-gp-text">{item.rationale}</p>
            </section>
          )}
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <p className="text-sm leading-6 text-gp-text-3">
            Группой можно сменить тип или удалить посадки. Обоснование по нормам показывается для одной посадки — выделите её кликом.
          </p>
        </div>
      )}

      {single && (
        <div className="flex-none border-t border-gp-line px-5 pb-4 pt-3">
          <Button variant="soft" className="w-full" onClick={onAskYuna}>
            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#3A4A2C] text-[11px] font-semibold text-gp-accent-text">Ю</span>
            Спросить Юну об этой посадке
          </Button>
        </div>
      )}
    </RightColumn>
  );
}
