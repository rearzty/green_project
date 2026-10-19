"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { VersionStats } from "@/components/CompareBar";
import type { CompareMarks } from "@/components/MapView";
import { getPlan, validatePlan, type PlanOut, type PlanSummary } from "@/lib/api";
import { diffPlans } from "@/lib/planDiff";
import { computePlanStats } from "@/lib/planStats";
import { errorMessage, toast } from "@/lib/toast";

const territoryOf = (plan: PlanOut) => plan.area_budget?.[0]?.territory_m2 ?? null;

function statsOf(plan: PlanOut, violations: number | null, geographic: boolean): VersionStats {
  const stats = computePlanStats(plan.features, geographic);
  const territory = territoryOf(plan);
  return { trees: stats.trees, violations, canopyShare: territory ? Math.min(1, stats.canopyM2 / territory) : null };
}

/** Режим «Сравнить версии» (Compare.dc.html, п. 5). Прежняя версия (`baseId`)
 * подгружается рядом с открытой: её посадки нужны только для разницы, поэтому
 * в сессию (`useProjectSession`) она не попадает и правка её не касается.
 * «Новая» версия — это просто открытый на экране план: её смена идёт через
 * обычный `onSelectPlan`, так что карта, нарушения и история остаются верными.
 *
 * Пока идёт сравнение, план только читают (page.tsx: `canEditPlan`, хоткеи,
 * меню и инспектор выключены) — иначе правка менялась бы под «разницей». */
export function useCompareVersions({
  projectId,
  plan,
  plans,
  geographic,
  currentViolations,
  onSelectPlan,
}: {
  projectId: string | undefined;
  plan: PlanOut | null;
  plans: PlanSummary[];
  geographic: boolean;
  /** Число посадок с нарушениями у открытой версии (из usePlanEdits); null — проверка идёт. */
  currentViolations: number | null;
  onSelectPlan: (planId: string) => Promise<void>;
}) {
  const [baseId, setBaseId] = useState<string | null>(null);
  const [base, setBase] = useState<{ plan: PlanOut; violations: number | null } | null>(null);
  const [busy, setBusy] = useState(false);
  // Устаревший ответ (версию успели сменить) в состояние не кладём.
  const seq = useRef(0);

  const active = baseId !== null;
  const ordered = useMemo(() => [...plans].sort((a, b) => a.created_at.localeCompare(b.created_at)), [plans]);

  /** Войти в сравнение: открытую версию — с предыдущей по времени (или со следующей, если открыта первая). */
  function start() {
    if (!plan || ordered.length < 2) return;
    const at = ordered.findIndex((p) => p.plan_id === plan.plan_id);
    const other = ordered[at > 0 ? at - 1 : at + 1] ?? ordered.find((p) => p.plan_id !== plan.plan_id);
    if (other) setBaseId(other.plan_id);
  }

  function exit() {
    seq.current += 1;
    setBaseId(null);
    setBase(null);
  }

  // Прежняя версия + её нарушения. Нарушения — полная проверка (как при
  // открытии плана): сравнивать «61 → 68» с чужой цифрой нечестно.
  useEffect(() => {
    if (!projectId || !baseId) return;
    const mine = ++seq.current;
    setBase(null);
    (async () => {
      try {
        const loaded = await getPlan(projectId, baseId);
        if (seq.current !== mine) return;
        setBase({ plan: loaded, violations: null });
        const { violations } = await validatePlan(projectId, baseId);
        if (seq.current !== mine) return;
        // Одна посадка может нарушать норму дважды — считаем посадки.
        setBase({ plan: loaded, violations: new Set(violations.map((v) => v.item_id)).size });
      } catch (e) {
        if (seq.current !== mine) return;
        toast.error(`Не удалось открыть версию для сравнения: ${errorMessage(e)}`);
        setBaseId(null);
      }
    })();
  }, [projectId, baseId]);

  // Версию удалили или сменили проект — сравнивать нечего.
  useEffect(() => {
    if (baseId && !plans.some((p) => p.plan_id === baseId)) exit();
    // exit читает только ref и сеттеры
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plans, baseId]);
  useEffect(() => {
    exit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const diff = useMemo(() => (base && plan ? diffPlans(base.plan.features, plan.features, geographic) : null), [base, plan, geographic]);
  const marks = useMemo<CompareMarks | null>(() => (diff ? { added: diff.added, removed: diff.removed } : null), [diff]);
  const baseStats = base ? statsOf(base.plan, base.violations, geographic) : null;
  const currentStats = plan ? statsOf(plan, currentViolations, geographic) : null;

  async function changeBase(planId: string) {
    // Выбрали ту же версию, что открыта справа, — меняем стороны, а не сравниваем её с собой.
    if (plan && planId === plan.plan_id && baseId) {
      await changeCurrent(baseId);
      return;
    }
    setBaseId(planId);
  }

  async function changeCurrent(planId: string) {
    if (!plan || planId === plan.plan_id) return;
    const wasBase = planId === baseId;
    const previousCurrent = plan.plan_id;
    setBusy(true);
    try {
      await onSelectPlan(planId);
      if (wasBase) setBaseId(previousCurrent);
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  /** «Вернуться к N»: открыть прежнюю версию и выйти. */
  async function revert() {
    if (!baseId) return;
    setBusy(true);
    try {
      await onSelectPlan(baseId);
      exit();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return {
    active,
    baseId,
    loading: active && (base === null || diff === null),
    busy,
    marks,
    unchanged: diff?.unchanged ?? 0,
    baseStats,
    currentStats,
    start,
    exit,
    changeBase,
    changeCurrent,
    revert,
  };
}
