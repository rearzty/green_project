"use client";

import { useMemo, useState } from "react";
import { Check, Loader2, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { LinkRow, PanelTitle, SectionLabel } from "@/components/panel/primitives";
import type { AreaBudget, ItemCompliance, PlanSummary, PlantingType, ValidationViolation } from "@/lib/api";
import { formatDistancePair, OBJECT_GENITIVE_RU, PLANTING_TYPE_LABELS } from "@/lib/format";
import { useViolationDetails } from "@/lib/hooks/useViolationDetails";
import { PLANTING_COLORS } from "@/lib/mapStyle";
import type { PlanStats } from "@/lib/planStats";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** Сколько нарушений показываем сразу: за остальными — «Показать ещё». Список
 * на реальной улице бывает в сотни строк, а действовать надо по одной. */
const PAGE = 20;

const fmt = (n: number) => Math.round(n).toLocaleString("ru-RU");

/** Что именно нарушено в строке: самое «не дотягивающее» непройденное
 * ограничение посадки. Нарушение интервала до соседнего дерева отличаем по
 * тексту сообщения (backend/app/services/edit_service.py::_spacing_violations)
 * — у одной посадки бывают оба вида, и строки не должны называться одинаково. */
export function pickViolatedCheck(details: ItemCompliance | undefined, message: string) {
  if (!details) return undefined;
  const wantNeighbour = message.includes("соседн");
  const failed = details.checks.filter((c) => !c.satisfied);
  const pool = failed.filter((c) => (c.object_type === "neighbour_tree") === wantNeighbour);
  const candidates = (pool.length > 0 ? pool : failed).sort((a, b) => a.actual_m - a.required_m - (b.actual_m - b.required_m));
  return candidates[0];
}

function IssueRow({
  violation,
  details,
  index,
  total,
  plantingType,
  active,
  disabled,
  onClick,
}: {
  violation: ValidationViolation;
  details: ItemCompliance | undefined;
  index: number;
  total: number;
  plantingType?: PlantingType;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const check = pickViolatedCheck(details, violation.message);
  const type = details?.planting_type ?? plantingType;
  const noun = type ? PLANTING_TYPE_LABELS[type] : "Посадка";
  const title = check ? `${noun} у ${OBJECT_GENITIVE_RU[check.object_type] ?? check.object_label}` : violation.message;
  const pair = check ? formatDistancePair(check.actual_m, check.required_m) : null;
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-current={active ? "true" : undefined}
      className={cn(
        "flex w-full items-start gap-3 px-5 py-3 text-left hover:bg-gp-hover focus-visible:outline-offset-[-2px] disabled:cursor-not-allowed disabled:opacity-60",
        active && "bg-gp-violation-row shadow-[inset_2px_0_0_#FF9351] hover:bg-gp-violation-row"
      )}
    >
      <span className="mt-1.5 h-2 w-2 flex-none rounded-full bg-gp-violation" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block text-[15px] font-medium leading-5 text-gp-text">{title}</span>
        {pair && (
          <span className={cn("mt-1 block font-mono text-xs tabular-nums", active ? "text-gp-violation-muted" : "text-gp-text-2")}>
            {pair.actual} · норма {pair.required}
          </span>
        )}
      </span>
      <span className="mt-0.5 flex-none font-mono text-xs tabular-nums text-gp-text-3">
        {index + 1} / {total}
      </span>
    </button>
  );
}

const BUDGET_TYPE_LABELS: Record<string, string> = { tree: "Деревья", shrub: "Кустарник", lawn: "Газон" };

/** Why a planting type got few (or no) plantings: the share of the
 * territory still open to it and the constraints that closed the rest. On a
 * dense street the strict setback tables leave trees almost nowhere to go --
 * without this, "only shrubs" reads as a generator bug instead of what the
 * norms actually say. */
function AreaBudgetList({ budgets }: { budgets: AreaBudget[] }) {
  return (
    <div className="flex flex-col gap-4 px-5 pb-4 pt-3">
      <p className="text-[13px] leading-5 text-gp-text-3">Сколько площади оставили нормы и что закрыло остальное</p>
      {budgets.map((budget) => {
        const scarce = budget.usable_share < 0.05;
        return (
          <div key={budget.planting_type}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-medium text-gp-text">{BUDGET_TYPE_LABELS[budget.planting_type] ?? budget.planting_type}</span>
              <span className={cn("font-mono text-xs tabular-nums", scarce ? "text-gp-warn" : "text-gp-text-2")}>
                {fmt(budget.usable_m2)} из {fmt(budget.territory_m2)} м² · {(100 * budget.usable_share).toFixed(1)}%
              </span>
            </div>
            <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-gp-line">
              <div
                className={cn("h-full rounded-full", scarce && "bg-gp-warn-bar")}
                style={{ width: `${Math.max(1, budget.usable_share * 100)}%`, backgroundColor: scarce ? undefined : PLANTING_COLORS[budget.planting_type] }}
              />
            </div>
            {budget.blockers.length > 0 && (
              <p className="mt-1.5 text-xs leading-5 text-gp-text-3">
                Больше всего закрывают: {budget.blockers.slice(0, 3).map((b) => `${b.label} ${Math.round(100 * b.share)}%`).join(", ")}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatPlanTime(plan: PlanSummary): string {
  return new Date(plan.created_at).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export interface CheckPanelProps {
  projectId?: string;
  planId?: string;
  planRevision: number;
  hasPlan: boolean;
  validating: boolean;
  /** null = not checked yet for the current plan. */
  violations: ValidationViolation[] | null;
  /** Всего посадок в плане — для «Остальные N посадок в норме». */
  itemCount: number;
  /** Тип каждой посадки по id: строка нарушения называется «Дерево у …» ещё
   * до того, как пришли подробности. */
  typeOf: (itemId: string) => PlantingType | undefined;
  stats: PlanStats | null;
  /** PlanOut.area_budget of the shown plan. */
  areaBudget?: AreaBudget[];
  showPlan: boolean;
  /** Единственная выделенная посадка — строка нарушения подсвечивается. */
  selectedItemId?: string;
  editBusy: boolean;
  onFocusItem: (itemId: string) => void;
  plans: PlanSummary[];
  currentPlanId?: string;
  onSelectPlan: (planId: string) => Promise<void>;
  /** Permanent -- the backend refuses the current plan (409), so its delete
   * button is disabled rather than surfacing the error. */
  onDeletePlan: (planId: string) => Promise<void>;
  exporting?: boolean;
  onExportDxf: () => Promise<void>;
  onDownloadComplianceReport?: (format: "json" | "csv") => void;
}

/** Вкладка «Проверка»: что нужно исправить (сверху — то, что требует
 * действия), состав плана, справочные строки и один главный экшен внизу
 * (Main.dc.html). Заменила «Проверку и экспорт» аккордеона и плавающую
 * PlanSummaryCard. */
export function CheckPanel({
  projectId,
  planId,
  planRevision,
  hasPlan,
  validating,
  violations,
  itemCount,
  typeOf,
  stats,
  areaBudget = [],
  showPlan,
  selectedItemId,
  editBusy,
  onFocusItem,
  plans,
  currentPlanId,
  onSelectPlan,
  onDeletePlan,
  exporting = false,
  onExportDxf,
  onDownloadComplianceReport,
}: CheckPanelProps) {
  const [shown, setShown] = useState(PAGE);
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [budgetOpen, setBudgetOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // Plan-history delete is permanent -- a second click on the same row
  // confirms it (no native window.confirm(), see toast.ts).
  const [confirmDeletePlanId, setConfirmDeletePlanId] = useState<string | null>(null);

  const list = violations ?? [];
  const visible = useMemo(() => list.slice(0, shown), [list, shown]);
  const details = useViolationDetails(projectId, planId, planRevision, visible.map((v) => v.item_id));

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

  const territoryM2 = areaBudget[0]?.territory_m2;
  // Покрытие кронами (tree canopy cover) — стандартный показатель городского
  // озеленения. Крона может нависать над тротуаром или отступом, поэтому
  // делим на весь участок, а не на площадь, где разрешено стоять стволу.
  const canopyShare = stats && territoryM2 && territoryM2 > 0 ? Math.min(1, stats.canopyM2 / territoryM2) : null;
  const percent = (v: number) => `${Math.round(v * 100)}%`;

  const treeBudget = areaBudget.find((b) => b.planting_type === "tree");
  const treesScarce = treeBudget ? treeBudget.usable_share < 0.05 : false;
  const okCount = Math.max(0, itemCount - new Set(list.map((v) => v.item_id)).size);

  if (!hasPlan) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <PanelTitle title="Проверка" hint="Появится, когда будет построен план" />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <section aria-labelledby="check-todo" className="pb-3 pt-5">
          <SectionLabel id="check-todo" className="mx-5 mb-2">
            {violations !== null && list.length === 0 ? "Проверка" : "Нужно исправить"}
          </SectionLabel>
          {validating && violations === null ? (
            <p className="mx-5 flex items-center gap-2 py-2 text-sm text-gp-text-2">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              Проверяю нормативы…
            </p>
          ) : violations === null ? (
            <p className="mx-5 py-2 text-sm text-gp-text-3">Нормативы ещё не проверены</p>
          ) : list.length === 0 ? (
            <p className="mx-5 flex items-center gap-2 py-1 text-[15px] text-gp-text">
              <Check className="h-4 w-4 flex-none text-gp-ok-strong" strokeWidth={2.2} aria-hidden />
              Все посадки в норме
            </p>
          ) : (
            <>
              <ul>
                {visible.map((violation, index) => (
                  <li key={`${violation.item_id}:${index}`}>
                    <IssueRow
                      violation={violation}
                      details={details.get(violation.item_id)}
                      index={index}
                      total={list.length}
                      plantingType={typeOf(violation.item_id)}
                      active={selectedItemId === violation.item_id}
                      disabled={!showPlan}
                      onClick={() => onFocusItem(violation.item_id)}
                    />
                  </li>
                ))}
              </ul>
              {list.length > shown && (
                <button
                  onClick={() => setShown((n) => n + PAGE)}
                  className="mx-5 mt-1 h-9 text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover"
                >
                  Показать ещё {Math.min(PAGE, list.length - shown)}
                </button>
              )}
              <p className="mx-5 mt-2.5 flex items-center gap-2 text-[13px] text-gp-text-3">
                <Check className="h-4 w-4 flex-none text-gp-ok-strong" strokeWidth={2.2} aria-hidden />
                Остальные {fmt(okCount)} посадок в норме
              </p>
            </>
          )}
        </section>

        {stats && (
          <section aria-labelledby="check-compo" className="border-t border-gp-line px-5 pb-5 pt-4">
            <SectionLabel id="check-compo" className="mb-1.5">
              Состав плана
            </SectionLabel>
            <dl className="text-sm">
              <div className="flex items-center gap-2.5 py-1.5">
                <span className="h-2.5 w-2.5 flex-none rounded-full" style={{ backgroundColor: PLANTING_COLORS.tree }} aria-hidden />
                <dt className="flex-1 text-gp-text">Деревья</dt>
                <dd className="font-mono tabular-nums">{fmt(stats.trees)}</dd>
              </div>
              <div className="flex items-center gap-2.5 py-1.5">
                <span className="h-2.5 w-2.5 flex-none rounded-full" style={{ backgroundColor: PLANTING_COLORS.shrub }} aria-hidden />
                <dt className="flex-1 text-gp-text">Кустарник</dt>
                <dd className="font-mono tabular-nums">{fmt(stats.shrubs)}</dd>
              </div>
              <div className="flex items-center gap-2.5 py-1.5">
                <span className="h-2.5 w-2.5 flex-none" style={{ background: "rgba(140,200,110,.35)", boxShadow: "inset 0 0 0 1px rgba(140,200,110,.7)" }} aria-hidden />
                <dt className="flex-1 text-gp-text">Газон</dt>
                <dd className="font-mono tabular-nums">{fmt(stats.lawnAreaM2)} м²</dd>
              </div>
              {canopyShare !== null && stats.trees > 0 && (
                <div className="flex items-center gap-2.5 py-1.5 text-gp-text-2">
                  <span className="w-2.5 flex-none" />
                  <dt
                    className="flex-1"
                    title="Сумма площадей крон деревьев (по диаметру кроны породы) к площади участка — оценка сверху, без вычета перекрытий"
                  >
                    Покрытие кронами
                  </dt>
                  <dd className="font-mono tabular-nums">{percent(canopyShare)}</dd>
                </div>
              )}
              {stats.designedShare !== null && stats.trees > 0 && (
                <div className="flex items-center gap-2.5 py-1.5 text-gp-text-2">
                  <span className="w-2.5 flex-none" />
                  <dt className="flex-1" title="Ряды вдоль улиц, группы и солитеры — схемы 743-ПП, а не россыпь">
                    Деревьев по схеме
                  </dt>
                  <dd className="font-mono tabular-nums">{percent(stats.designedShare)}</dd>
                </div>
              )}
              {stats.speciesCount > 0 && (
                <div className="flex items-center gap-2.5 py-1.5 text-gp-text-2">
                  <span className="w-2.5 flex-none" />
                  <dt className="flex-1">Пород</dt>
                  <dd className="font-mono tabular-nums">{stats.speciesCount}</dd>
                </div>
              )}
            </dl>
          </section>
        )}

        {areaBudget.length > 0 && (
          <>
            <LinkRow onClick={() => setBudgetOpen((v) => !v)}>{treesScarce ? "Почему деревьев мало" : "Где можно сажать"}</LinkRow>
            {budgetOpen && <AreaBudgetList budgets={areaBudget} />}
          </>
        )}

        <LinkRow value={plans.length} onClick={() => setVersionsOpen((v) => !v)} last={!versionsOpen}>
          Версии плана
        </LinkRow>
        {versionsOpen && (
          <ul className="flex max-h-60 flex-col overflow-y-auto border-b border-gp-line px-2 py-2 text-[13px]">
            {plans.map((p) => (
              <li key={p.plan_id} className="flex items-center gap-1">
                <button
                  disabled={busy || editBusy}
                  onClick={() => guarded(() => onSelectPlan(p.plan_id))}
                  aria-current={p.plan_id === currentPlanId ? "true" : undefined}
                  className={cn(
                    "flex min-h-10 w-full min-w-0 items-center justify-between gap-2 rounded-lg px-3 text-left disabled:opacity-50",
                    p.plan_id === currentPlanId ? "bg-gp-accent-soft" : "hover:bg-gp-hover"
                  )}
                >
                  <span className="truncate text-gp-text">
                    {p.scoring_mode === "ml" ? "ML-модель" : "Формула"} · <span className="font-mono tabular-nums">{formatPlanTime(p)}</span>
                    {p.is_current && <span className="ml-1.5 text-gp-accent">· последний</span>}
                  </span>
                  <span className="flex-none font-mono tabular-nums text-gp-text-2">{p.item_count.toLocaleString("ru-RU")}</span>
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
                  title={p.is_current ? "Нельзя удалить последний план" : confirmDeletePlanId === p.plan_id ? "Точно удалить?" : "Удалить версию"}
                  aria-label={confirmDeletePlanId === p.plan_id ? "Точно удалить версию?" : "Удалить версию"}
                  className={cn(
                    "flex h-10 w-10 flex-none items-center justify-center rounded-lg disabled:opacity-30",
                    confirmDeletePlanId === p.plan_id ? "bg-gp-violation-soft text-gp-violation-text" : "text-gp-text-3 hover:bg-gp-hover hover:text-gp-violation-text"
                  )}
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex-none border-t border-gp-line px-5 pb-4 pt-4">
        <p className="mb-3 text-[13px] leading-5 text-gp-text-3">Экспорт возможен и с нарушениями — они попадут в отчёт отдельным списком.</p>
        <Button size="lg" className="w-full" disabled={busy || exporting} onClick={() => guarded(() => onExportDxf())}>
          {exporting && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
          {exporting ? "Экспортируем…" : "Скачать DXF с подосновой"}
        </Button>
        <div className="mt-1 flex gap-1">
          <Button
            variant="ghost"
            size="sm"
            className="flex-1 font-medium"
            disabled={!onDownloadComplianceReport}
            onClick={() => onDownloadComplianceReport?.("json")}
            title="Обоснование каждой посадки со ссылкой на акт и пункт"
          >
            Отчёт обоснования
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className="flex-1 font-medium"
            disabled={!onDownloadComplianceReport}
            onClick={() => onDownloadComplianceReport?.("csv")}
            title="То же построчно: посадка × норматив — для Excel"
          >
            CSV для Excel
          </Button>
        </div>
      </div>
    </div>
  );
}
