"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckCheck,
  PanelLeftClose,
  PanelLeftOpen,
  Redo2,
  Shrub,
  SquareDashedMousePointer,
  Trash2,
  TreeDeciduous,
  Undo2,
  X,
} from "lucide-react";

import { AssistantChat } from "@/components/AssistantChat";
import { ContextMenu, type ContextMenuEntry } from "@/components/ContextMenu";
import { ControlPanel } from "@/components/ControlPanel";
import { InspectorCard, type InspectedItem } from "@/components/InspectorCard";
import type { MapFitRequest } from "@/components/MapView";
import { PlanSummaryCard } from "@/components/PlanSummaryCard";
import { Toaster } from "@/components/Toaster";
import {
  fetchComplianceReport,
  getItemsCompliance,
  getProjectLayers3d,
  PROJECT_GONE_MESSAGE,
  type AssistantAction,
  type AssistantCommand,
  type AssistantGenerationSettings,
  type AssistantSelection,
  type GeoJSONFeatureCollection,
  type ItemCompliance,
  type PlantingType,
  type ScoringMode,
} from "@/lib/api";
import {
  countText,
  describeSettingsChange,
  doneText,
  idsForTarget,
  missingText,
  planCountsText,
  PLANTING,
  PLANTING_ACC,
  PLANTING_GEN,
  selectedText,
  settingsFromAction,
  violationsSelectedText,
  type CommandResult,
  type SelectTarget,
  type UiAction,
} from "@/lib/assistantCommands";
import { countLabel, OBJECT_FORMS } from "@/lib/format";
import { useKeyboardShortcuts } from "@/lib/hooks/useKeyboardShortcuts";
import { usePlanEdits } from "@/lib/hooks/usePlanEdits";
import { useProjectSession } from "@/lib/hooks/useProjectSession";
import { useSelection } from "@/lib/hooks/useSelection";
import type { Season } from "@/lib/mapStyle";
import { boundsOfItems } from "@/lib/planIndex";
import { recipeForPlan } from "@/lib/planRecipes";
import { computePlanStats } from "@/lib/planStats";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

// Leaflet touches `window` on import, so the map must never render on the server.
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });
// Same reasoning as MapView -- three.js's WebGLRenderer needs a real DOM/GPU context.
const ThreeDView = dynamic(() => import("@/components/ThreeDView"), { ssr: false });

// Итоги команд Юны, которые повторяются в нескольких местах исполнителя.
const NO_PLAN = "Плана ещё нет — сначала сгенерируйте его.";
const NOTHING_SELECTED = "Ничего не выделено — выделите посадки на карте или попросите, например, «выдели все деревья».";
const EDIT_BUSY = "Ещё сохраняется предыдущая правка — попробуйте через пару секунд.";
const OTHER_OPERATION = "Сейчас идёт другая операция — попробуйте через пару секунд.";
const UNKNOWN_COMMAND = "Эту команду я пока не умею выполнять.";
const SELECTION_CHANGED = "Выделение изменилось, пока Юна отвечала, — правка не выполнена. Повторите просьбу.";
const NEEDS_ORDER_BOUNDARY_CONSENT =
  "План не построен: границы работ в поставке нет, строить по границе заказа на съёмку можно только с вашего согласия. Настройки — в панели слева, подтвердите там «Да, по границе заказа».";
/** Правка стартует в ближайшем рендере; не видно её и через это время —
 * runEdit отказался начинать (уже шла другая), ждать нечего. */
const EDIT_START_TIMEOUT_MS = 3000;
/** Батч-правка по id — секунды даже на большом выделении; дольше — уже не ждём. */
const EDIT_FINISH_TIMEOUT_MS = 120_000;
/** Полная проверка нового плана на нормативы — секунды; дольше итог Юны не
 * держим, нарушения всё равно покажет сводка плана. */
const VALIDATION_TIMEOUT_MS = 20_000;
const NO_IDS: ReadonlySet<string> = new Set();

/** Карточки над картой справа (сводка плана, выделение): right-4 + w-72 и
 * зазор до чата — столько чат Юны оставляет им у правого края окна. */
const CARDS_RESERVED_PX = 16 + 288 + 8;

function sameIds(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a === b) return true;
  if (a.size !== b.size) return false;
  for (const id of a) if (!b.has(id)) return false;
  return true;
}

export default function Home() {
  const session = useProjectSession();
  const { project, layersRaster, plan, plans, planRevision, planIndex } = session;

  const [hiddenLayerTypes, setHiddenLayerTypes] = useState<Set<string>>(new Set());
  const [showPlan, setShowPlan] = useState(true);
  const [view3d, setView3d] = useState(false);
  const [season, setSeason] = useState<Season>("summer");
  // ThreeDView is the only remaining consumer of the full vector layer
  // geometry (its building extrusion needs real polygons, not a raster) --
  // fetched lazily on the first toggle into 3D rather than unconditionally
  // on every project open, which is what ProjectOut used to do (see its
  // docstring: 170MB/60s+ on a real 371K-layer project). Keyed by project id
  // so switching projects re-fetches and switching back to 2D just hides
  // the view without discarding what's already loaded.
  const [layers3d, setLayers3d] = useState<{ projectId: string; layers: GeoJSONFeatureCollection } | undefined>(undefined);

  useEffect(() => {
    if (!view3d || !project) return;
    if (layers3d?.projectId === project.id) return;
    let cancelled = false;
    getProjectLayers3d(project.id)
      .then((layers) => {
        if (!cancelled) setLayers3d({ projectId: project.id, layers });
      })
      .catch((error) => {
        if (!cancelled) toast.error(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [view3d, project, layers3d?.projectId]);
  // Lifted out of ControlPanel (used to be its own local state) so
  // AssistantChat can read and change the same generation recipe Юна talks
  // about -- see ControlPanel.tsx's own comment on why this moved.
  const [plantingTypes, setPlantingTypes] = useState<PlantingType[]>(["tree", "shrub", "lawn"]);
  const [scoringMode, setScoringMode] = useState<ScoringMode>("heuristic");
  // undefined = not overridden -- generate omits the field and the backend
  // falls back to planting_norms.yaml's own tree_default/shrub_default.
  const [treeSpacingM, setTreeSpacingM] = useState<number | undefined>(undefined);
  const [shrubSpacingM, setShrubSpacingM] = useState<number | undefined>(undefined);
  const [treeRows, setTreeRows] = useState(1);
  const [treesInPaving, setTreesInPaving] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  // «Покажи эти объекты» для карты — Юна выделила группу (selectByAssistant).
  const [fitRequest, setFitRequest] = useState<MapFitRequest | null>(null);
  // Юну попросили построить план, а нужен ответ на вопрос панели о границе
  // заказа (ControlPanel.tsx::consentRequest) — счётчик, каждый — новый вопрос.
  const [consentRequest, setConsentRequest] = useState(0);
  // Defaults open (matches the always-visible panel this replaces) and is
  // corrected once on mount for phones -- SSR has no window to check, so a
  // static default that's occasionally wrong for one frame beats guessing.
  const [sidebarOpen, setSidebarOpen] = useState(true);

  useEffect(() => {
    if (!window.matchMedia("(min-width: 768px)").matches) setSidebarOpen(false);
  }, []);
  // Sourced from the raster endpoint's own grouping now, not recomputed from
  // project.layers on the client -- layer_raster.py already walks every
  // layer once to build the same key/label/color/count, and project.layers
  // itself can be hundreds of thousands of features (see MapView's backdrop
  // switch below); redoing that walk here on every project load for a
  // number the backend already has was pure waste.
  const layerLegend = layersRaster?.groups ?? [];

  function handleToggleLayerType(key: string) {
    setHiddenLayerTypes((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // editing needs selection.setSelection (to clear/restore selection around
  // an edit), selection needs editing.violationIds (to show a selected
  // item's violation status) -- passing a closure here instead of
  // `selection` itself sidesteps the chicken-and-egg ordering: the closure
  // isn't actually *called* until well after this render finishes (in
  // response to a later edit), by which point `selection` below is assigned.
  const editing = usePlanEdits(project, plan, session.applyLocalEdit, session.bumpPlanRevision, session.patchPlanItemCount, (ids) =>
    selection.setSelection(ids)
  );
  const selection = useSelection(planIndex, planRevision, editing.violationIds);

  // Обоснование ("Почему здесь?", ControlPanel.tsx) -- только для ровно одной
  // выделенной точки: geo_engine.compliance.explain_items не зависит от
  // остальных посадок, так что запрашивать его для группы бессмысленно (что
  // показывать -- обоснование какой из них?). Тот же id, что уже выделен на
  // карте -- не отдельный источник правды.
  const [complianceForSelection, setComplianceForSelection] = useState<ItemCompliance | null>(null);
  const [complianceLoading, setComplianceLoading] = useState(false);
  const singleSelectedId = selection.selectedIds.size === 1 ? [...selection.selectedIds][0] : undefined;

  useEffect(() => {
    if (!project || !plan || !singleSelectedId) {
      setComplianceForSelection(null);
      return;
    }
    let cancelled = false;
    // Сразу сбрасываем: иначе карточка новой посадки до ответа показывала бы
    // обоснование предыдущей.
    setComplianceForSelection(null);
    setComplianceLoading(true);
    getItemsCompliance(project.id, plan.plan_id, [singleSelectedId])
      .then((items) => {
        if (!cancelled) setComplianceForSelection(items[0] ?? null);
      })
      .catch(() => {
        if (!cancelled) setComplianceForSelection(null);
      })
      .finally(() => {
        if (!cancelled) setComplianceLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [project, plan, singleSelectedId]);

  /** Отчёт обоснований по плану на экране — и для кнопок панели, и для Юны.
   * Оба формата — сначала в память (api.ts::fetchComplianceReport), потом
   * ссылкой на blob: при ошибке сервера прямая ссылка уводила браузер со
   * страницы вместе с разговором Юны, а чат при этом писал «скачивается».
   * Промис завершается, когда файл уже отдан браузеру, — либо ошибкой с
   * готовым текстом. */
  async function downloadComplianceReport(format: "json" | "csv") {
    const { project: currentProject, plan: currentPlan } = runtimeRef.current.session;
    if (!currentProject || !currentPlan) return;
    let blob: Blob;
    try {
      blob = await fetchComplianceReport(currentProject.id, currentPlan.plan_id, format);
    } catch (e) {
      throw new Error(`Отчёт не скачан: ${errorMessage(e)}`);
    }
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = `compliance_${currentPlan.plan_id}.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Не сразу: часть браузеров начинает читать blob уже после click().
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  }

  function toggleView3d() {
    // Turning it off is always allowed, same reasoning as toggleSelectMode
    // below -- only turning it on needs a plan to actually show.
    if (view3d) {
      setView3d(false);
      return;
    }
    if (!plan) return;
    setView3d(true);
  }

  function handleToggleShowPlan(show: boolean) {
    setShowPlan(show);
    if (!show) {
      selection.setSelectMode(false);
      selection.clear();
    }
  }

  function toggleSelectMode() {
    // Turning it off is always allowed -- only turning it on needs a
    // visible plan. Otherwise a plan disappearing (Clear All, switching
    // projects) while select mode was on could leave it stuck with no way
    // to switch it back off.
    if (selection.selectMode) {
      selection.setSelectMode(false);
      setContextMenu(null);
      return;
    }
    if (!plan) return;
    if (!showPlan) {
      toast.info("Включите отображение плана, чтобы выделять объекты.");
      return;
    }
    selection.setSelectMode(true);
    setContextMenu(null);
  }

  function handleEscape() {
    if (contextMenu) setContextMenu(null);
    else if (selection.selectedIds.size > 0) selection.clear();
    else if (selection.selectMode) selection.setSelectMode(false);
  }

  const closeContextMenu = useCallback(() => setContextMenu(null), []);
  const handleContextMenu = useCallback((x: number, y: number) => setContextMenu({ x, y }), []);

  useKeyboardShortcuts({
    undo: () => editing.undo(),
    redo: editing.redo,
    deleteSelection: () => editing.handleDeleteSelection(selection.selectedIds),
    retype: (type) => editing.handleRetypeSelection(selection.selectedIds, planIndex, type),
    toggleSelectMode,
    selectAll: selection.selectAll,
    escape: handleEscape,
    hasPlan: plan !== null,
    hasSelection: selection.selectedIds.size > 0,
  });

  const hasSelection = selection.selectedIds.size > 0;

  const planStats = useMemo(
    // Градусы — только если карта и правда в WGS84: та же проверка, что
    // backend's geo_io.display_crs (подтверждённая CRS и она задана).
    () => (plan ? computePlanStats(plan.features, Boolean(project?.crs_verified && project?.source_crs)) : null),
    [plan, project?.crs_verified, project?.source_crs]
  );

  // Properties of the single selected feature for InspectorCard (species,
  // crown, rationale) -- looked up in the current plan.features, which every
  // edit path keeps in sync, rather than a separately-stale copy.
  const inspectedItem = useMemo<InspectedItem | undefined>(() => {
    if (!plan || !singleSelectedId) return undefined;
    const feature = plan.features.features.find((f) => (f.properties as { id?: string }).id === singleSelectedId);
    return feature?.properties as InspectedItem | undefined;
  }, [plan, singleSelectedId]);

  // --- Юна: исполнение её команд ---
  //
  // Команды одного ответа идут по очереди и ждут друг друга («перегенерируй и
  // выдели деревья»), а правки (usePlanEdits) и генерация не возвращают промис
  // со своим итогом — он виден только по следующим рендерам. Поэтому
  // исполнитель читает состояние через runtimeRef, свежий на каждом рендере,
  // а не из замыкания того рендера, в котором пришёл ответ, и ждёт нужного
  // состояния через waitForRender.

  const generationSettings: AssistantGenerationSettings = {
    planting_types: plantingTypes,
    tree_spacing_m: treeSpacingM,
    shrub_spacing_m: shrubSpacingM,
    scoring_mode: scoringMode,
    tree_rows: treeRows,
    trees_in_paving: treesInPaving,
  };
  const runtime = { session, editing, selection, planStats, view3d, settings: generationSettings };
  const runtimeRef = useRef(runtime);
  runtimeRef.current = runtime;

  const renderWaitersRef = useRef(new Set<() => void>());
  useEffect(() => {
    renderWaitersRef.current.forEach((check) => check());
  });

  /** Ждёт рендера, после которого predicate() истинно; false — не дождались. */
  function waitForRender(predicate: () => boolean, timeoutMs: number): Promise<boolean> {
    return new Promise((resolve) => {
      const waiters = renderWaitersRef.current;
      const finish = (done: boolean) => {
        clearTimeout(timer);
        waiters.delete(check);
        resolve(done);
      };
      const check = () => {
        if (predicate()) finish(true);
      };
      const timer = setTimeout(() => finish(false), timeoutMs);
      waiters.add(check);
      check();
    });
  }

  /** Правка usePlanEdits запускается как `void runEdit(...)`, и её итог виден
   * только по рендерам: editBusy поднимается и опускается, а planRevision
   * растёт и при успехе (эффект применён локально), и при отказе
   * (перемонтирование слоя) — что именно вышло, вызывающий смотрит сам по
   * planIndex. */
  async function waitForEdit(revisionBefore: number): Promise<"done" | "not_started" | "timeout"> {
    let sawBusy = false;
    const busyNow = () => {
      const busy = runtimeRef.current.editing.editBusy;
      if (busy) sawBusy = true;
      return busy;
    };
    const started = await waitForRender(() => {
      busyNow();
      return sawBusy || runtimeRef.current.session.planRevision !== revisionBefore;
    }, EDIT_START_TIMEOUT_MS);
    if (!started) return "not_started";
    return (await waitForRender(() => !busyNow(), EDIT_FINISH_TIMEOUT_MS)) ? "done" : "timeout";
  }

  /** Отмена правки из чата — тот же Ctrl+Z, но только пока после неё план не
   * менялся: иначе undo() снял бы со стека чужую, более позднюю правку. */
  function editUndo(isReverted: () => boolean): () => Promise<void> {
    const revisionAfter = runtimeRef.current.session.planRevision;
    return async () => {
      const { session: now, editing: edits } = runtimeRef.current;
      if (now.planRevision !== revisionAfter) throw new Error("После этого план уже менялся — отменяйте по шагам: Ctrl+Z.");
      if (edits.editBusy || now.generating) throw new Error(OTHER_OPERATION);
      edits.undo();
      if ((await waitForEdit(revisionAfter)) !== "done" || !isReverted()) {
        throw new Error("Отменить не получилось — причина в уведомлении.");
      }
    };
  }

  function applyGenerationSettings(next: AssistantGenerationSettings) {
    setPlantingTypes(next.planting_types);
    setTreeSpacingM(next.tree_spacing_m ?? undefined);
    setShrubSpacingM(next.shrub_spacing_m ?? undefined);
    setScoringMode(next.scoring_mode);
    setTreeRows(next.tree_rows);
    setTreesInPaving(next.trees_in_paving);
  }

  // Поля панели — рецепт плана на экране, как только на экране другой план:
  // восстановлен после обновления страницы, выбран в «Версиях плана», только
  // что построен. Иначе после обновления двухрядный план показывался с
  // выключенной аллеей, и следующая просьба к Юне («кусты пореже») молча
  // строила однорядный — она меняет то, что видит в полях. Правка того же
  // плана рецепт не меняет — эффект только на смену plan_id.
  useEffect(() => {
    if (plan) applyGenerationSettings(recipeForPlan(plan, runtimeRef.current.settings));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.plan_id]);

  /** Число посадок с нарушениями у плана на экране — когда закончится его
   * полная проверка (её сам запускает usePlanEdits при смене плана); null —
   * не дождались или проверка не удалась (о ней тогда скажет уведомление). */
  async function violatingItemsOf(planId: string): Promise<number | null> {
    await waitForRender(() => runtimeRef.current.editing.checkedPlanId === planId, VALIDATION_TIMEOUT_MS);
    const edits = runtimeRef.current.editing;
    return edits.checkedPlanId === planId && edits.violations !== null ? edits.violationIds.size : null;
  }

  /** Новый рецепт генерации — тем же путём, что кнопка панели (история
   * планов, сессия, общий индикатор `generating`), а поля панели сразу
   * показывают то, что поставила Юна. Отмена — обратно к плану, который был
   * на экране до команды, и к прежним полям панели. */
  async function applySettingsByAssistant(action: AssistantAction): Promise<CommandResult> {
    const { session: current, editing: edits, settings: prev } = runtimeRef.current;
    if (!current.project) return { ok: false, text: "Сначала загрузите чертёж — план строить не на чем." };
    if (current.generating) return { ok: false, text: "Уже идёт генерация — дождитесь её." };
    if (edits.editBusy) return { ok: false, text: EDIT_BUSY };
    const next = settingsFromAction(action, prev);
    if (next.planting_types.length === 0) return { ok: false, text: "Нечего сажать: оставьте хотя бы один тип посадки." };
    // Границы работ в поставке нет: строить по границе заказа можно только с
    // согласия, и давать его за пользователя Юна не вправе. Тот же вопрос,
    // что задаёт кнопка панели, — с настройками Юны уже в полях: «Да» там
    // построит именно то, о чём просили в чате.
    if (current.project.territory_source === "order_boundary" && !current.project.use_order_boundary) {
      applyGenerationSettings(next);
      setSidebarOpen(true);
      setConsentRequest((n) => n + 1);
      return { ok: false, text: NEEDS_ORDER_BOUNDARY_CONSENT };
    }
    const changes = describeSettingsChange(prev, next);
    const prevPlanId = current.plan?.plan_id;
    applyGenerationSettings(next);
    let failure: unknown = null;
    try {
      await current.handleGenerate(next.planting_types, next.scoring_mode, {
        treeSpacingM: next.tree_spacing_m,
        shrubSpacingM: next.shrub_spacing_m,
        treeRows: next.tree_rows,
        treesInPaving: next.trees_in_paving,
      });
    } catch (e) {
      failure = e;
    }
    const after = runtimeRef.current;
    const newPlanId = after.session.plan?.plan_id;
    // handleGenerate падает и после того, как новый план уже на экране (не
    // обновился список версий) — тогда план всё-таки построен.
    if (failure !== null && (!newPlanId || newPlanId === prevPlanId)) {
      // План на экране не поменялся — и панель обратно к его рецепту.
      applyGenerationSettings(prev);
      return { ok: false, text: `План не построен: ${errorMessage(failure)}` };
    }
    const counts = after.planStats ? planCountsText(after.planStats) : "";
    // Генератор ставит посадки только по нормам, но нарушения бывают и у
    // нового плана (интервал между фазами посадки, см. CLAUDE.md), — тогда
    // об этом говорит и итог, а не одна сводка в углу карты.
    const violating = newPlanId ? await violatingItemsOf(newPlanId) : null;
    const violationsNote = violating ? `. Посадок с нарушениями норм: ${violating.toLocaleString("ru-RU")} — отмечены красным` : "";
    // Тот же рецепт и никаких ручных правок — бэкенд отдаёт текущий план
    // заново (pipeline_service._find_reusable_plan), отменять тут нечего.
    if (prevPlanId && newPlanId === prevPlanId) {
      return { ok: true, text: `План с такими настройками уже на экране${counts ? `: ${counts}` : ""}${violationsNote}` };
    }
    const head = changes.length > 0 ? changes.join("; ") : prevPlanId ? "План построен заново с теми же настройками" : "План построен";
    const undo =
      prevPlanId && newPlanId
        ? async () => {
            const { session: now, editing: nowEdits } = runtimeRef.current;
            if (now.generating || nowEdits.editBusy) throw new Error(OTHER_OPERATION);
            if (now.plan?.plan_id !== newPlanId) {
              throw new Error("На экране уже другой план — нужную версию можно выбрать в панели, «Версии плана».");
            }
            await now.handleSelectPlan(prevPlanId);
            applyGenerationSettings(prev);
          }
        : undefined;
    return { ok: true, text: `${counts ? `${head}. В плане: ${counts}` : head}${violationsNote}`, undo };
  }

  /** Правку выделенного пользователь должен видеть: в 3D нет ни выделения,
   * ни карточки (ThreeDView — только просмотр), а правка оттуда меняла бы
   * то, чего на экране нет. Так же, как выделение (selectByAssistant). */
  function showEditedOnMap() {
    setShowPlan(true);
    setView3d(false);
  }

  async function deleteByAssistant(ids: ReadonlySet<string>): Promise<CommandResult> {
    const { session: current, editing: edits } = runtimeRef.current;
    const index = current.planIndex;
    if (!current.plan || !index) return { ok: false, text: NO_PLAN };
    const targets = [...ids].filter((id) => index.has(id));
    if (targets.length === 0) return { ok: false, text: NOTHING_SELECTED };
    if (edits.editBusy) return { ok: false, text: EDIT_BUSY };
    showEditedOnMap();
    const revision = current.planRevision;
    edits.handleDeleteSelection(new Set(targets));
    const outcome = await waitForEdit(revision);
    if (outcome === "not_started") return { ok: false, text: "Удаление не началось — попробуйте ещё раз." };
    if (outcome === "timeout") return { ok: true, text: `Удаляю ${countText(targets.length, PLANTING_ACC)} — итог появится на карте.` };
    const stillThere = () => targets.filter((id) => runtimeRef.current.session.planIndex?.has(id)).length;
    const removed = targets.length - stillThere();
    if (removed === 0) return { ok: false, text: "Не удалось удалить — причина в уведомлении." };
    return { ok: true, text: doneText("Удален", removed, PLANTING), undo: editUndo(() => stillThere() === targets.length) };
  }

  async function retypeByAssistant(ids: ReadonlySet<string>, type: "tree" | "shrub"): Promise<CommandResult> {
    const { session: current, editing: edits } = runtimeRef.current;
    const index = current.planIndex;
    if (!current.plan || !index) return { ok: false, text: NO_PLAN };
    const targets = [...ids].filter((id) => index.has(id));
    if (targets.length === 0) return { ok: false, text: NOTHING_SELECTED };
    const noun = type === "tree" ? "деревья" : "кусты";
    // Что поменяется — так же, как считает handleRetypeSelection: точки
    // другого типа; газон — площадной объект, его тип не меняется.
    const previous = new Map<string, PlantingType>();
    let lawns = 0;
    targets.forEach((id) => {
      const item = index.get(id);
      if (!item) return;
      if (!item.isPoint) lawns += 1;
      else if (item.type !== type) previous.set(id, item.type);
    });
    if (previous.size === 0) {
      if (lawns === targets.length) return { ok: false, text: "Газон — площадной объект: деревом или кустом его не сделать." };
      return { ok: false, text: targets.length === 1 ? `Выделенная посадка уже ${type === "tree" ? "дерево" : "куст"}.` : `Выделенные посадки уже ${noun}.` };
    }
    if (edits.editBusy) return { ok: false, text: EDIT_BUSY };
    showEditedOnMap();
    const revision = current.planRevision;
    edits.handleRetypeSelection(new Set(targets), index, type);
    const outcome = await waitForEdit(revision);
    if (outcome === "not_started") return { ok: false, text: "Смена типа не началась — попробуйте ещё раз." };
    if (outcome === "timeout") return { ok: true, text: `Меняю тип у ${countText(previous.size, PLANTING_GEN)} — итог появится на карте.` };
    const countTyped = (expected: (id: string) => PlantingType | undefined) =>
      [...previous.keys()].filter((id) => runtimeRef.current.session.planIndex?.get(id)?.type === expected(id)).length;
    const changed = countTyped(() => type);
    if (changed === 0) return { ok: false, text: "Не удалось сменить тип — причина в уведомлении." };
    // «1 посадка → куст», но «3 посадки → кусты».
    const target = changed === 1 ? (type === "tree" ? "дерево" : "куст") : noun;
    return {
      ok: true,
      text: `${countText(changed, PLANTING.forms)} → ${target}${lawns > 0 ? " (газон остался газоном)" : ""}`,
      undo: editUndo(() => countTyped((id) => previous.get(id)) === previous.size),
    };
  }

  /** Выделение группы и рамка карты под неё. Синхронно: возвращает и то, что
   * выделено, — следующая команда того же ответа увидит его раньше рендера.
   * Не вышло (плана нет, нормативы не проверены, выделять нечего) — для
   * следующих команд ответа выделение пустое, а не прежнее: «выдели липы и
   * удали их» без единой липы иначе удалило бы то, что пользователь выделял
   * раньше. Само выделение на карте при этом не трогаем — команда ничего не
   * сделала. */
  function selectByAssistant(target: SelectTarget, species?: string | null): { result: CommandResult; selected: ReadonlySet<string> } {
    const { session: current, editing: edits, selection: sel } = runtimeRef.current;
    if (target === "none") {
      sel.clear();
      return { result: { ok: true, text: "Выделение снято" }, selected: NO_IDS };
    }
    const index = current.planIndex;
    if (!current.plan || !index) return { result: { ok: false, text: NO_PLAN }, selected: NO_IDS };
    let ids: string[];
    let text: string;
    if (target === "violations") {
      if (edits.violations === null) {
        const notYet = edits.validating ? "Нормативы ещё проверяются — спросите через пару секунд." : "Нормативы ещё не проверены.";
        return { result: { ok: false, text: notYet }, selected: NO_IDS };
      }
      ids = [...edits.violationIds].filter((id) => index.has(id));
      if (ids.length === 0) return { result: { ok: true, text: "Нарушений нет — все нормативные отступы соблюдены" }, selected: NO_IDS };
      text = violationsSelectedText(ids.length);
    } else {
      ids = idsForTarget(index, current.plan.features, target, species);
      if (ids.length === 0) return { result: { ok: false, text: missingText(target, species) }, selected: NO_IDS };
      text = selectedText(target, ids.length, species);
    }
    // Выделение видно только на 2D-карте с включённым планом. Без проверки
    // текущего вида: «открой 3D и выдели …» — один ответ, и runtimeRef ещё
    // не знает про 3D, а тот же самый вид React просто не перерисует.
    setShowPlan(true);
    setView3d(false);
    sel.setSelection(ids);
    const box = boundsOfItems(index, ids);
    if (box) setFitRequest({ box, ids });
    return { result: { ok: true, text }, selected: new Set(ids) };
  }

  async function uiByAssistant(command: UiAction): Promise<CommandResult> {
    const { session: current, view3d: in3d } = runtimeRef.current;
    switch (command) {
      case "export_dxf":
        if (!current.plan) return { ok: false, text: NO_PLAN };
        if (current.exporting) return { ok: false, text: "Экспорт уже идёт — файл скачается, когда он закончится." };
        await current.handleExportDxf();
        return { ok: true, text: "DXF с подосновой скачивается" };
      case "report_json":
      case "report_csv": {
        if (!current.plan) return { ok: false, text: NO_PLAN };
        const format = command === "report_json" ? "json" : "csv";
        // Ждёт, пока файл придёт целиком: «скачан» — только когда он уже у
        // браузера, ошибка сервера — строкой с ⚠, а не уходом со страницы.
        await downloadComplianceReport(format);
        return { ok: true, text: `Отчёт обоснований (${format.toUpperCase()}) скачан` };
      }
      case "show_3d":
        // Та же проверка, что у toggleView3d: 3D без плана показывать нечего.
        if (!current.plan) return { ok: false, text: "3D-вид появится вместе с планом — сначала сгенерируйте его." };
        setView3d(true);
        return { ok: true, text: in3d ? "3D-вид уже открыт" : "Открыт 3D-вид" };
      case "show_2d":
        setView3d(false);
        return { ok: true, text: in3d ? "Открыта карта" : "Карта уже на экране" };
      default:
        return { ok: false, text: UNKNOWN_COMMAND };
    }
  }

  /** Команды из ответа Юны — по порядку, теми же путями, что кнопки панели
   * и карточки. Ошибка одной команды — её строка с ⚠ в чате, не исключение.
   * `sentSelection` — выделение в момент, когда просьбу отправили
   * (AssistantChat.tsx::send): именно о нём Юна и слышала. */
  async function handleAssistantCommands(
    commands: AssistantCommand[],
    { selectedIds: sentSelection }: { selectedIds: ReadonlySet<string> }
  ): Promise<CommandResult[]> {
    const results: CommandResult[] = [];
    // Выделение между командами ведём сами: setSelection дойдёт до
    // runtimeRef только к следующему рендеру, а «выдели деревья и сделай их
    // кустами» — один ответ. Пока его не заменила команда «выдели …» этого
    // же ответа, это снимок с момента отправки — и править его можно, только
    // если на карте выделено то же самое: карта не заблокирована, пока Юна
    // отвечает, и Ctrl+A перед ответом на «удали выделенные» иначе удалил бы
    // всё.
    let selected = sentSelection;
    let fromSnapshot = true;
    for (const command of commands) {
      let result: CommandResult;
      try {
        switch (command.type) {
          case "settings": {
            const planBefore = runtimeRef.current.session.plan?.plan_id;
            result = await applySettingsByAssistant(command.settings);
            // Новый план — новые id: из прежнего выделения в нём нет ничего.
            if (runtimeRef.current.session.plan?.plan_id !== planBefore) {
              selected = NO_IDS;
              fromSnapshot = false;
            }
            break;
          }
          case "edit_selection":
            if (fromSnapshot && !sameIds(selected, runtimeRef.current.selection.selectedIds)) {
              result = { ok: false, text: SELECTION_CHANGED };
            } else if (command.operation === "delete") {
              result = await deleteByAssistant(selected);
              if (result.ok) {
                selected = NO_IDS;
                fromSnapshot = false;
              }
            } else if (command.operation === "to_tree" || command.operation === "to_shrub") {
              result = await retypeByAssistant(selected, command.operation === "to_tree" ? "tree" : "shrub");
            } else {
              result = { ok: false, text: UNKNOWN_COMMAND };
            }
            break;
          case "select": {
            const picked = selectByAssistant(command.target, command.species);
            result = picked.result;
            selected = picked.selected;
            fromSnapshot = false;
            break;
          }
          case "ui":
            result = await uiByAssistant(command.command);
            break;
          default:
            result = { ok: false, text: UNKNOWN_COMMAND };
        }
      } catch (e) {
        result = { ok: false, text: errorMessage(e) };
      }
      results.push(result);
    }
    return results;
  }

  // Без плана выделять нечего; без выделения — нули, чтобы на «удали
  // выделенные» Юна честно ответила, что ничего не выделено.
  const assistantSelection: AssistantSelection | undefined = plan
    ? {
        count: selection.summary?.total ?? 0,
        trees: selection.summary?.counts.tree ?? 0,
        shrubs: selection.summary?.counts.shrub ?? 0,
        lawns: selection.summary?.counts.lawn ?? 0,
      }
    : undefined;

  const contextMenuEntries: ContextMenuEntry[] = [
    {
      label: "Сделать деревом",
      hotkey: "1",
      icon: TreeDeciduous,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleRetypeSelection(selection.selectedIds, planIndex, "tree"),
    },
    {
      label: "Сделать кустарником",
      hotkey: "2",
      icon: Shrub,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleRetypeSelection(selection.selectedIds, planIndex, "shrub"),
    },
    {
      label: "Удалить",
      hotkey: "Del",
      icon: Trash2,
      danger: true,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleDeleteSelection(selection.selectedIds),
    },
    "separator",
    { label: "Выделить всё", hotkey: "Ctrl+A", icon: CheckCheck, disabled: !planIndex, onSelect: selection.selectAll },
    { label: "Снять выделение", hotkey: "Esc", icon: X, disabled: !hasSelection, onSelect: selection.clear },
    "separator",
    { label: "Отменить", hotkey: "Ctrl+Z", icon: Undo2, disabled: !editing.canUndo || editing.editBusy, onSelect: () => editing.undo() },
    { label: "Повторить", hotkey: "Ctrl+Y", icon: Redo2, disabled: !editing.canRedo || editing.editBusy, onSelect: editing.redo },
    "separator",
    {
      label: selection.selectMode ? "Выключить режим выделения" : "Включить режим выделения",
      hotkey: "S",
      icon: SquareDashedMousePointer,
      onSelect: toggleSelectMode,
    },
  ];

  // Карточки над картой справа — сводка плана (и в 3D) и выделение (только 2D).
  const cardsShown = planStats !== null || (!view3d && selection.summary !== null);

  return (
    <main className="flex h-full w-full">
      <div
        className={cn(
          "h-full flex-none overflow-hidden transition-all duration-200 ease-in-out",
          // Телефон: выезжающая шторка поверх карты — выше всего, что лежит
          // на карте (контролы Leaflet 1000, карточки 1050, чат Юны 1100), но
          // ниже уведомлений (1150), чтобы ошибка из панели была видна.
          "fixed inset-y-0 left-0 z-[1120] md:static md:z-auto",
          // Ширина обёртки на телефоне — ровно ширина самой панели
          // (ControlPanel: w-[88vw] max-w-96): прозрачный остаток w-96 на
          // 390 px экрана закрывал затемнённый фон, и закрыть шторку тапом
          // было можно только по последним 6 px.
          sidebarOpen ? "w-[88vw] max-w-96 translate-x-0 md:w-96" : "-translate-x-full md:w-0 md:translate-x-0"
        )}
      >
        <ControlPanel
          hasProject={project !== null}
          projectName={project?.name}
          projectWarnings={session.projectLost ? [PROJECT_GONE_MESSAGE, ...(project?.warnings ?? [])] : project?.warnings}
          needsOrderBoundaryConsent={project?.territory_source === "order_boundary" && !project?.use_order_boundary}
          consentRequest={consentRequest}
          areaBudget={plan?.area_budget}
          hasPlan={plan !== null}
          restoring={session.restoring}
          generating={session.generating}
          uploadProgress={session.uploadProgress}
          generateProgress={session.generateProgress}
          exporting={session.exporting}
          onUpload={session.handleUpload}
          onGenerate={session.handleGenerate}
          plantingTypes={plantingTypes}
          onPlantingTypesChange={setPlantingTypes}
          scoringMode={scoringMode}
          onScoringModeChange={setScoringMode}
          treeSpacingM={treeSpacingM}
          onTreeSpacingMChange={setTreeSpacingM}
          shrubSpacingM={shrubSpacingM}
          onShrubSpacingMChange={setShrubSpacingM}
          treeRows={treeRows}
          onTreeRowsChange={setTreeRows}
          treesInPaving={treesInPaving}
          onTreesInPavingChange={setTreesInPaving}
          onExportDxf={session.handleExportDxf}
          onDownloadComplianceReport={(format) => {
            downloadComplianceReport(format).catch((e) => toast.error(errorMessage(e)));
          }}
          onClearAll={session.handleClearAll}
          layerLegend={layerLegend}
          hiddenLayerTypes={hiddenLayerTypes}
          onToggleLayerType={handleToggleLayerType}
          showPlan={showPlan}
          onToggleShowPlan={handleToggleShowPlan}
          view3d={view3d}
          onToggleView3d={toggleView3d}
          season={season}
          onSeasonChange={setSeason}
          plans={plans}
          currentPlanId={plan?.plan_id}
          onSelectPlan={session.handleSelectPlan}
          onDeletePlan={session.handleDeletePlan}
          editBusy={editing.editBusy}
          canUndo={editing.canUndo}
          canRedo={editing.canRedo}
          onUndo={() => editing.undo()}
          onRedo={editing.redo}
          selectMode={selection.selectMode}
          onToggleSelectMode={toggleSelectMode}
          selection={selection.summary}
          onSelectAll={selection.selectAll}
          validating={editing.validating}
          violations={editing.violations}
          onFocusItem={selection.focusItem}
        />
      </div>
      {sidebarOpen && (
        <div className="fixed inset-0 z-[1110] bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} aria-hidden />
      )}
      <button
        onClick={() => setSidebarOpen((v) => !v)}
        title={sidebarOpen ? "Скрыть панель" : "Показать панель"}
        aria-pressed={sidebarOpen}
        className={cn(
          // top-24, not top-4 -- Leaflet's own zoom control sits at the
          // map's top-left corner (~10-74px from the top, confirmed live)
          // and renders above this button (leaflet's z-index 1000 > our
          // 500), silently eating clicks at a naive top-4 position.
          "fixed top-24 rounded-md border border-stone-700 bg-stone-900 p-1.5 text-stone-300 shadow transition-[left] duration-200 ease-in-out hover:bg-stone-800",
          // Открытая шторка телефона: кнопка — у её правого края (ширина та
          // же, что у панели) и над затемнением; left-[400px] уводил её за
          // край 390 px экрана.
          sidebarOpen ? "left-[calc(min(88vw,24rem)+0.5rem)] z-[1130] md:left-[400px] md:z-[500]" : "left-4 z-[500]"
        )}
      >
        {sidebarOpen ? <PanelLeftClose className="h-4 w-4" aria-hidden /> : <PanelLeftOpen className="h-4 w-4" aria-hidden />}
      </button>
      <div className="relative min-w-0 flex-1">
        {view3d ? (
          <ThreeDView
            layers={layers3d?.projectId === project?.id ? layers3d?.layers : undefined}
            plan={plan?.features}
            crsVerified={project?.crs_verified}
            season={season}
            storageKey={project?.id}
          />
        ) : (
          <MapView
            layersRaster={layersRaster}
            crsVerified={project?.crs_verified}
            plan={plan?.features}
            planIndex={planIndex}
            layersKey={project?.id}
            planId={plan?.plan_id}
            planRevision={planRevision}
            hiddenLayerTypes={hiddenLayerTypes}
            showPlan={showPlan}
            selectMode={selection.selectMode}
            selectedIds={selection.selectedIds}
            violationIds={editing.violationIds}
            editsLocked={editing.editBusy}
            onSelectionChange={selection.setSelection}
            onMoveItems={editing.handleMoveItems}
            onContextMenu={handleContextMenu}
            onViewChange={closeContextMenu}
            focusRequest={selection.focusRequest}
            fitRequest={fitRequest}
            sidebarOpen={sidebarOpen}
          />
        )}
        <div className="pointer-events-none absolute bottom-24 right-4 top-4 z-[1050] flex flex-col items-end gap-3">
          {/* Обе карточки помечены data-map-overlay сами (а не эта колонка во
              всю высоту): рамка «выдели …» (MapView.tsx::freeMapRects) может
              лечь и под ними, если там свободно. */}
          {planStats && (
            <PlanSummaryCard
              stats={planStats}
              violations={editing.violations ? editing.violationIds.size : null}
              validating={editing.validating}
              territoryM2={plan?.area_budget?.[0]?.territory_m2}
              scoringMode={plan?.scoring_mode}
            />
          )}
          {!view3d && selection.summary && (
            <InspectorCard
              key={singleSelectedId ?? "group"}
              selection={selection.summary}
              item={inspectedItem}
              compliance={complianceForSelection}
              complianceLoading={complianceLoading}
              canEdit={plan !== null && !editing.editBusy}
              onRetype={(type) => editing.handleRetypeSelection(selection.selectedIds, planIndex, type)}
              onDelete={() => editing.handleDeleteSelection(selection.selectedIds)}
              onClear={selection.clear}
            />
          )}
        </div>
      </div>
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          title={hasSelection ? `Выделено: ${countLabel(selection.selectedIds.size, OBJECT_FORMS)}` : "Ничего не выделено"}
          entries={contextMenuEntries}
          onClose={closeContextMenu}
        />
      )}
      <Toaster />
      <AssistantChat
        projectId={project?.id}
        generating={session.generating}
        settings={generationSettings}
        planId={plan?.plan_id}
        selectedItemId={singleSelectedId}
        selection={assistantSelection}
        selectedIds={selection.selectedIds}
        // Посадок с нарушениями, а не записей о нарушениях: у одной посадки
        // их бывает две (отступ и интервал), а Юна говорит о посадках.
        violationsCount={editing.violations ? editing.violationIds.size : null}
        view={view3d ? "3d" : "2d"}
        planTrees={planStats?.trees ?? 0}
        sidebarOpen={sidebarOpen}
        reservedRight={cardsShown ? CARDS_RESERVED_PX : 0}
        projectLost={session.projectLost}
        onCommands={handleAssistantCommands}
      />
    </main>
  );
}
