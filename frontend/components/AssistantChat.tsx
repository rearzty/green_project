"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronRight, Eraser, Loader2, X } from "lucide-react";

import {
  ApiError,
  askAssistant,
  getAssistantStatus,
  isProjectLost,
  PROJECT_GONE_MESSAGE,
  type AssistantChatMessage,
  type AssistantCommand,
  type AssistantGenerationSettings,
  type AssistantMessageResult,
  type AssistantSelection,
  type AssistantStatus,
} from "@/lib/api";
import { RightColumn } from "@/components/RightColumn";
import { suggestionChips, type CommandResult } from "@/lib/assistantCommands";
import { errorMessage } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** Итог одной команды под ответом Юны. Сама функция отмены живёт в undoFns
 * компонента: в состоянии чата — только то, что рисуется. */
interface ResultLine {
  ok: boolean;
  text: string;
  undo?: "available" | "running" | "done";
  undoError?: string;
}

interface ChatEntry {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** "rules" — ответил встроенный разбор команд, без языковой модели. */
  mode?: "llm" | "rules";
  notice?: string | null;
  results?: ResultLine[];
  /** Запрос не прошёл (сеть, сервер) — в историю для Юны не идёт. */
  isError?: boolean;
  /** Что отправить заново по «Повторить» — только у ошибок. */
  retryText?: string;
}

export interface AssistantChatProps {
  /** Юна needs a project to talk about -- the assistant endpoint is nested
   * under /api/projects/{project_id} for consistency with every other route
   * (see routes_assistant.py). Undefined before one's loaded: the widget
   * still opens, but the input stays disabled with an explanatory placeholder. */
  projectId?: string;
  /** Shared with the control panel's own "Генерация…" indicator -- a
   * generate triggered from the panel and one triggered from chat are the
   * same underlying job, never two at once. */
  generating: boolean;
  settings: AssistantGenerationSettings;
  /** План на экране и выделенная посадка — по ним Юна отвечает по фактам. */
  planId?: string;
  selectedItemId?: string;
  /** Состав выделения; undefined — плана нет. */
  selection?: AssistantSelection;
  /** Сами выделенные id — снимок уходит исполнителю вместе с командами
   * ответа (см. send): «удали выделенные» — про то, что было выделено, когда
   * просьбу отправили, а не про то, что выделено секунды спустя. */
  selectedIds: ReadonlySet<string>;
  /** null — план ещё не проверен на нормативы. */
  violationsCount: number | null;
  view: "2d" | "3d";
  /** Деревьев в плане на экране — для подсказки «Выдели все деревья». */
  planTrees: number;
  /** Чат — правая колонка страницы (app/page.tsx), открывается кнопкой в
   * шапке. Компонент смонтирован всегда, даже закрытым: разговоры проектов,
   * отмены команд (undoFns) и идущий запрос живут в его состоянии, и
   * закрытие колонки не должно их терять. */
  open: boolean;
  onClose: () => void;
  /** Юна ответила, пока колонка была закрыта: у кнопки в шапке появляется точка. */
  onUnread: () => void;
  /** Как называется то, что выделено, — для метки-контекста у поля ввода
   * («Липа мелколистная», «14 объектов»). */
  selectionLabel?: string;
  /** Вопрос извне (палитра команд, Tab): каждый новый `seq` отправляется как
   * сообщение от пользователя. Чат смонтирован всегда, так что вопрос уйдёт и
   * при закрытой колонке — она открывается страницей. */
  askRequest?: { text: string; seq: number } | null;
  /** Сервер ответил, что этого проекта у него больше нет (перезапуск). */
  projectLost?: boolean;
  /** Выполнить команды ответа по порядку (app/page.tsx); не бросает —
   * ошибка команды приходит её строкой с ok: false. `selectedIds` —
   * выделение на момент отправки просьбы. */
  onCommands: (commands: AssistantCommand[], context: { selectedIds: ReadonlySet<string> }) => Promise<CommandResult[]>;
}

/** Сколько последних реплик уходит Юне историей: запрос не растёт вместе с
 * разговором, а для «сделай их кустами» хватает и пары последних. */
const HISTORY_LIMIT = 20;
const NO_ENTRIES: ChatEntry[] = [];
const NO_IDS: ReadonlySet<string> = new Set();
/** Сколько готовых вопросов показываем под разговором. */
const MAX_QUESTION_ROWS = 4;

let entrySeq = 0;
// Не crypto.randomUUID: его нет вне secure context, а фронт открывают и по
// http с LAN-адреса (телефон в той же сети, см. api.ts::API_URL).
function newEntryId(): string {
  entrySeq += 1;
  return `${Date.now().toString(36)}-${entrySeq}`;
}

function toHistory(entries: ChatEntry[]): AssistantChatMessage[] {
  const history: AssistantChatMessage[] = [];
  entries.forEach((entry, i) => {
    if (entry.isError) return;
    // Вопрос, на который вместо ответа пришла ошибка, Юна не видела.
    if (entry.role === "user" && entries[i + 1]?.isError) return;
    history.push({ role: entry.role, content: entry.content });
  });
  return history.slice(-HISTORY_LIMIT);
}

/** Команды ответа. Бэкенд до списка команд присылал только рецепт
 * генерации (поле action) — такой ответ тоже понимаем. */
function commandsOf(result: AssistantMessageResult): AssistantCommand[] {
  if (Array.isArray(result.actions)) return result.actions;
  return result.action ? [{ type: "settings", settings: result.action }] : [];
}

/** **Жирный** и переносы строк (whitespace-pre-wrap у пузыря) — вся
 * разметка, которой пользуется Юна. Без dangerouslySetInnerHTML: текст
 * приходит от языковой модели. */
function RichText({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\*\*[^*\n]+\*\*)/g).map((part, i) =>
        part.length > 4 && part.startsWith("**") && part.endsWith("**") ? (
          <strong key={i} className="font-semibold text-gp-text">
            {part.slice(2, -2)}
          </strong>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        )
      )}
    </>
  );
}

function ModeBadge({ status }: { status: AssistantStatus }) {
  if (status.llm_configured) {
    // «openai/gpt-oss-20b» → «gpt-oss-20b»: в шапке тесно, полное имя — в подсказке.
    const model = status.model?.split("/").pop() || status.provider;
    return (
      <span
        className="min-w-0 truncate rounded-full bg-gp-accent-soft px-2 py-px font-mono text-[10px] font-medium text-gp-accent-text"
        title={`Отвечает языковая модель ${status.model ?? status.provider}. Если она недоступна, Юна понимает команды из подсказок.`}
      >
        ИИ · {model}
      </span>
    );
  }
  return (
    <span className="flex-none rounded-full bg-gp-line px-2 py-px text-[10px] text-gp-text-2" title="Без ключа ИИ она понимает команды из подсказок">
      базовый режим
    </span>
  );
}

/** Готовый вопрос/команда строкой под разговором: 40px, шеврон справа
 * (Yuna.dc.html, `.q`). */
function QuestionRow({ text, disabled, onClick }: { text: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex min-h-10 w-full items-center justify-between gap-2.5 rounded-lg px-3 text-left text-sm text-gp-text hover:bg-gp-hover disabled:opacity-40"
    >
      {text}
      <ChevronRight className="h-4 w-4 flex-none text-gp-text-3" aria-hidden />
    </button>
  );
}

/** Чат с Юной — правая колонка (RightColumn), а не плавающее окно слева
 * снизу: карта сужается, а не прячется под чатом. Юна отвечает
 * текстом и командами; команды выполняет страница (onCommands) теми же
 * путями, что кнопки, а под ответом остаются их итоги с «Отменить».
 *
 * Разговор хранится по проекту в памяти вкладки: при переключении проекта
 * возвращается его разговор, а обновление страницы чат забывает — так
 * просил пользователь (CLAUDE.md, раздел про Юну). Бэкенд по-прежнему ничего
 * не хранит: история уходит с каждым сообщением. */
export function AssistantChat({
  projectId,
  generating,
  settings,
  planId,
  selectedItemId,
  selection,
  selectedIds,
  violationsCount,
  view,
  planTrees,
  open,
  onClose,
  onUnread,
  selectionLabel,
  askRequest = null,
  projectLost = false,
  onCommands,
}: AssistantChatProps) {
  // Разговор вместе с проектом, к которому он относится, — одним значением:
  // иначе эффект сохранения успевал бы записать разговор старого проекта
  // под новый (та же ловушка, что у истории правок, см. CLAUDE.md про undo).
  const [chat, setChat] = useState<{ projectId?: string; entries: ChatEntry[] }>({ entries: NO_ENTRIES });
  const [draft, setDraft] = useState("");
  const [phase, setPhase] = useState<"idle" | "thinking" | "acting">("idle");
  const [undoing, setUndoing] = useState(false);
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  // /yuna-avatar.png isn't checked into the repo (it's the user's own art,
  // dropped into frontend/public/ by hand) -- falls back to a plain "Ю"
  // initial via onError so the widget works identically before and after
  // that file shows up, no code change needed either way.
  const [avatarError, setAvatarError] = useState(false);
  // Метка-контекст у поля ввода снята крестиком: пока выделение то же самое,
  // Юне оно не передаётся. useSelection на каждое изменение создаёт новый Set,
  // так что «то же самое» — это та же ссылка, и новое выделение метку
  // возвращает само.
  const [dismissedContext, setDismissedContext] = useState<ReadonlySet<string> | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Ref, а не модульная переменная: Fast Refresh пересоздаёт модуль, но не
  // состояние компонента, и разговоры проектов пропадали бы при правке кода.
  const transcriptsRef = useRef(new Map<string, ChatEntry[]>());
  const undoFns = useRef(new Map<string, () => void | Promise<void>>());
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;
  const openRef = useRef(open);
  openRef.current = open;
  // Поле ввода не блокируется, пока Юна думает, и два быстрых Enter прошли
  // бы проверку `busy` одного и того же рендера — отсюда отдельный флаг.
  const sendingRef = useRef(false);

  const entries = chat.projectId === projectId ? chat.entries : NO_ENTRIES;
  const busy = phase !== "idle" || generating || undoing;
  // Проекта на сервере больше нет: любая просьба ответила бы тем же 404.
  const canTalk = Boolean(projectId) && !projectLost;

  // Что Юна услышит про выделение. Метку сняли — нули, чтобы на «удали
  // выделенные» она честно ответила, что ничего не выделено, а не работала с
  // тем, от чего пользователь сам отказался.
  const contextOn = selectedIds.size > 0 && dismissedContext !== selectedIds;
  const sentSelectedIds: ReadonlySet<string> = contextOn ? selectedIds : NO_IDS;
  const sentSelectedItemId = contextOn ? selectedItemId : undefined;
  const sentSelection: AssistantSelection | undefined = contextOn
    ? selection
    : selection
      ? { count: 0, trees: 0, shrubs: 0, lawns: 0 }
      : undefined;

  useEffect(() => {
    // Отмены — замыкания над планом прошлого проекта, к этому не относятся.
    undoFns.current.clear();
    setChat({ projectId, entries: (projectId && transcriptsRef.current.get(projectId)) || NO_ENTRIES });
    setConfirmClear(false);
  }, [projectId]);

  useEffect(() => {
    if (chat.projectId) transcriptsRef.current.set(chat.projectId, chat.entries);
  }, [chat]);

  useEffect(() => {
    if (!open || status) return;
    let cancelled = false;
    getAssistantStatus()
      .then((next) => {
        if (!cancelled) setStatus(next);
      })
      .catch(() => {
        // Без статуса просто нет значка режима — отвечать Юне это не мешает.
      });
    return () => {
      cancelled = true;
    };
  }, [open, status]);

  // Вниз — на новую реплику, итоги команд и индикатор, но не на смену
  // строки «Отменить» где-то выше: пользователь мог прокрутить к ней сам.
  const lastEntry = entries[entries.length - 1];
  const scrollKey = `${entries.length}:${lastEntry?.results?.length ?? 0}`;
  useEffect(() => {
    if (open) scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [scrollKey, open, busy]);

  // Поле растёт вместе с текстом (onInput ниже); после отправки текст
  // сброшен программно, onInput не срабатывает — высоту возвращаем сами.
  useEffect(() => {
    if (draft === "" && inputRef.current) inputRef.current.style.height = "auto";
  }, [draft]);

  useEffect(() => {
    // Только при открытии, не после каждого ответа: фокус в поле глушит
    // горячие клавиши карты (useKeyboardShortcuts пропускает текстовые поля),
    // а пока Юна думала, пользователь мог уйти править карту. И только с
    // мышью — на телефоне фокус сразу поднимал бы клавиатуру.
    if (open && window.matchMedia("(pointer: fine)").matches) inputRef.current?.focus();
  }, [open]);

  function appendEntry(forProject: string, entry: ChatEntry) {
    setChat((prev) => (prev.projectId === forProject ? { ...prev, entries: [...prev.entries, entry] } : prev));
    if (entry.role === "assistant" && !openRef.current) onUnread();
  }

  function patchEntry(forProject: string, id: string, patch: (entry: ChatEntry) => ChatEntry) {
    setChat((prev) =>
      prev.projectId === forProject ? { ...prev, entries: prev.entries.map((entry) => (entry.id === id ? patch(entry) : entry)) } : prev
    );
  }

  async function send(preset?: string) {
    const text = (preset ?? draft).trim();
    if (!text || !projectId || !canTalk || busy || sendingRef.current) return;
    sendingRef.current = true;
    if (preset === undefined) setDraft("");
    const forProject = projectId;
    // Выделение, о котором Юна сейчас услышит (`selection` — его состав).
    // Пока она отвечает, карта не заблокирована: Ctrl+A до ответа на «удали
    // выделенные» не должен превратить его в «удали всё». useSelection
    // каждый раз создаёт новый Set, так что ссылка — уже снимок.
    const snapshotIds = sentSelectedIds;
    // The backend holds no conversation state of its own (see
    // assistant_service.py) -- the transcript rides along on every message,
    // built from what's already on screen rather than a second copy.
    const history = toHistory(entries);
    appendEntry(forProject, { id: newEntryId(), role: "user", content: text });
    setPhase("thinking");
    try {
      let result: AssistantMessageResult;
      try {
        result = await askAssistant(forProject, text, history, settings, {
          planId,
          selectedItemId: sentSelectedItemId,
          selection: sentSelection,
          violationsCount,
          view,
        });
      } catch (e) {
        // Единственный 404 этого маршрута — «проекта нет» (ProjectDep):
        // сервер перезапускался, и «Повторить» не поможет никогда.
        const gone = e instanceof ApiError && (e.projectGone || e.status === 404);
        appendEntry(forProject, {
          id: newEntryId(),
          role: "assistant",
          content: gone ? PROJECT_GONE_MESSAGE : errorMessage(e),
          isError: true,
          retryText: gone ? undefined : text,
        });
        return;
      }
      // Пока Юна думала, открыли другой проект — её ответ и команды не про него.
      if (projectIdRef.current !== forProject) return;
      const entryId = newEntryId();
      const commands = commandsOf(result);
      appendEntry(forProject, { id: entryId, role: "assistant", content: result.reply, mode: result.mode, notice: result.notice ?? null });
      if (commands.length === 0) return;
      setPhase("acting");
      let results: CommandResult[];
      try {
        results = await onCommands(commands, { selectedIds: snapshotIds });
      } catch (e) {
        results = [{ ok: false, text: errorMessage(e) }];
      }
      if (projectIdRef.current !== forProject) return;
      results.forEach((r, i) => {
        if (r.undo) undoFns.current.set(`${entryId}:${i}`, r.undo);
      });
      patchEntry(forProject, entryId, (entry) => ({
        ...entry,
        results: results.map((r) => ({ ok: r.ok, text: r.text, undo: r.undo ? "available" : undefined })),
      }));
      if (!openRef.current) onUnread();
    } finally {
      sendingRef.current = false;
      setPhase("idle");
    }
  }

  function retry(entry: ChatEntry) {
    const text = entry.retryText;
    if (!text || !projectId || !canTalk || busy) return;
    const forProject = projectId;
    // Убираем и ошибку, и вопрос перед ней — send() задаст его заново.
    setChat((prev) => {
      if (prev.projectId !== forProject) return prev;
      const index = prev.entries.findIndex((e) => e.id === entry.id);
      if (index < 0) return prev;
      const before = prev.entries[index - 1];
      const start = before?.role === "user" && before.content === text ? index - 1 : index;
      return { ...prev, entries: [...prev.entries.slice(0, start), ...prev.entries.slice(index + 1)] };
    });
    void send(text);
  }

  async function runUndo(entryId: string, index: number) {
    const key = `${entryId}:${index}`;
    const undo = undoFns.current.get(key);
    if (!undo || !projectId || busy) return;
    const forProject = projectId;
    const setLine = (patch: Partial<ResultLine>) =>
      patchEntry(forProject, entryId, (entry) => ({
        ...entry,
        results: entry.results?.map((line, i) => (i === index ? { ...line, ...patch } : line)),
      }));
    // Отмена — такой же запрос к серверу, а проекта там уже нет.
    if (projectLost) {
      setLine({ undoError: PROJECT_GONE_MESSAGE });
      return;
    }
    setLine({ undo: "running", undoError: undefined });
    setUndoing(true);
    try {
      await undo();
      undoFns.current.delete(key);
      setLine({ undo: "done" });
    } catch (e) {
      // Отмена правки сообщает лишь «причина в уведомлении» (сам запрос
      // идёт через usePlanEdits) — потерю проекта называем здесь же.
      setLine({ undo: "available", undoError: isProjectLost(forProject) ? PROJECT_GONE_MESSAGE : errorMessage(e) });
    } finally {
      setUndoing(false);
    }
  }

  function clearChat() {
    if (!projectId) return;
    undoFns.current.clear();
    transcriptsRef.current.delete(projectId);
    setChat({ projectId, entries: NO_ENTRIES });
    setConfirmClear(false);
  }

  function avatar(className: string) {
    if (avatarError) {
      return <span className={cn(className, "flex items-center justify-center rounded-full bg-[#3A4A2C] font-semibold text-gp-accent-text")}>Ю</span>;
    }
    return <img src="/yuna-avatar.png" alt="Юна" className={cn(className, "rounded-full object-cover")} onError={() => setAvatarError(true)} />;
  }

  // Вопрос извне (палитра команд): каждый новый seq — одно сообщение.
  // Обработанный seq запоминаем в ref, а не в состоянии: повторный рендер с тем
  // же запросом не должен отправить его второй раз.
  const handledAskRef = useRef(0);
  useEffect(() => {
    if (!askRequest || askRequest.seq === handledAskRef.current) return;
    handledAskRef.current = askRequest.seq;
    void send(askRequest.text);
    // send — замыкание этого рендера; запрос обрабатывается ровно один раз
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askRequest?.seq]);

  const chips = suggestionChips({
    hasProject: canTalk,
    hasPlan: Boolean(planId),
    settings,
    planTrees,
    violationsCount,
    selection: sentSelection,
  });

  function renderResult(entry: ChatEntry, line: ResultLine, index: number) {
    const canUndo = undoFns.current.has(`${entry.id}:${index}`);
    return (
      <li key={index} className="flex flex-col gap-0.5">
        <div className="flex items-start gap-2 text-[13px] leading-5">
          {line.ok ? (
            <Check className="mt-0.5 h-3.5 w-3.5 flex-none text-gp-ok-strong" aria-label="Выполнено" />
          ) : (
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-gp-warn" aria-label="Не выполнено" />
          )}
          <span className={cn("min-w-0 flex-1", line.ok ? "text-gp-text-2" : "text-gp-warn", line.undo === "done" && "text-gp-text-4 line-through")}>
            {line.text}
          </span>
          {line.undo === "done" ? (
            <span className="flex-none text-gp-text-3">отменено</span>
          ) : canUndo && line.undo === "running" ? (
            <span className="flex-none text-gp-text-3">отменяю…</span>
          ) : canUndo && line.undo === "available" ? (
            <button
              onClick={() => runUndo(entry.id, index)}
              disabled={busy}
              className="flex-none font-medium text-gp-accent underline-offset-2 hover:underline disabled:opacity-40"
            >
              Отменить
            </button>
          ) : null}
        </div>
        {line.undoError && <p className="pl-[22px] text-xs leading-5 text-gp-warn">{line.undoError}</p>}
      </li>
    );
  }

  // Одно и то же пояснение (например, «ключ ИИ не задан») под каждым ответом
  // подряд — шум: показываем, только когда оно сменилось.
  let lastNotice: string | null = null;

  // Закрытая колонка не рисуется, но компонент остаётся смонтированным —
  // см. комментарий к `open` в AssistantChatProps.
  if (!open) return null;

  return (
    <RightColumn label="Юна">
      <div className="flex items-center gap-2.5 border-b border-gp-line py-2.5 pl-5 pr-2">
        {avatar("h-7 w-7 flex-none text-[13px]")}
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <p className="flex-none text-[15px] font-medium text-gp-text">Юна</p>
          {status && <ModeBadge status={status} />}
        </div>
        <button
          onClick={() => (confirmClear ? clearChat() : setConfirmClear(true))}
          onBlur={() => setConfirmClear(false)}
          disabled={entries.length === 0 || busy}
          title={confirmClear ? "Нажмите ещё раз — чат очистится" : "Очистить чат"}
          aria-label={confirmClear ? "Очистить чат — нажмите ещё раз" : "Очистить чат"}
          className={cn(
            "flex h-9 w-9 flex-none items-center justify-center rounded-md transition-colors disabled:opacity-30",
            confirmClear ? "bg-gp-violation-soft text-gp-violation-text" : "text-gp-text-2 hover:bg-gp-hover hover:text-gp-text"
          )}
        >
          <Eraser className="h-4 w-4" aria-hidden />
        </button>
        <button onClick={onClose} className="flex h-9 w-9 flex-none items-center justify-center rounded-md text-gp-text-2 hover:bg-gp-hover hover:text-gp-text" aria-label="Закрыть">
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-5" aria-live="polite">
        {entries.length === 0 ? (
          <div className="flex flex-col gap-3">
            <p className="text-[15px] leading-6 text-gp-text">
              Привет! Я Юна. Объясню план и поменяю его: перестрою с другими настройками, выделю и поправлю посадки, выгружу DXF или отчёт обоснований.
            </p>
            <p className="text-[13px] leading-5 text-gp-text-3">
              {!projectId
                ? "Сначала загрузите чертёж на вкладке «Чертёж»."
                : projectLost
                  ? PROJECT_GONE_MESSAGE
                  : status && !status.llm_configured
                    ? "Сейчас я в базовом режиме, без ИИ: понимаю команды вроде подсказок ниже."
                    : "Выделите посадку на карте — расскажу, почему она стоит именно там."}
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-[18px]">
            {entries.map((entry) => {
              if (entry.role === "user") {
                // Пузырь — только у сообщений пользователя.
                return (
                  <div
                    key={entry.id}
                    className="max-w-[85%] self-end whitespace-pre-wrap break-words rounded-[14px_14px_4px_14px] bg-[#34402C] px-3.5 py-2.5 text-[15px] leading-[1.45] text-gp-text"
                  >
                    {entry.content}
                  </div>
                );
              }
              if (entry.isError) {
                return (
                  <div key={entry.id} role="alert" className="flex flex-col gap-1.5 rounded-lg border border-gp-violation-soft bg-gp-violation-row px-3 py-2.5 text-sm text-gp-violation-text">
                    <p className="flex gap-2">
                      <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" aria-hidden />
                      <span className="min-w-0 break-words">{entry.content}</span>
                    </p>
                    {entry.retryText && (
                      <button onClick={() => retry(entry)} disabled={busy} className="self-start pl-6 text-[13px] font-medium underline-offset-2 hover:underline disabled:opacity-40">
                        Повторить
                      </button>
                    )}
                  </div>
                );
              }
              const notice = entry.notice && entry.notice !== lastNotice ? entry.notice : null;
              if (entry.notice) lastNotice = entry.notice;
              return (
                <div key={entry.id} className="flex flex-col gap-2.5">
                  {/* Ответ Юны — обычным текстом без пузыря (Yuna.dc.html). */}
                  <div className="whitespace-pre-wrap break-words text-[15px] leading-[1.6] text-gp-text">
                    <RichText text={entry.content} />
                  </div>
                  {entry.results && entry.results.length > 0 && <ul className="flex flex-col gap-1.5">{entry.results.map((line, i) => renderResult(entry, line, i))}</ul>}
                  {(entry.mode === "rules" || notice) && (
                    <p className="flex items-start gap-1.5 text-xs leading-5 text-gp-text-3">
                      {entry.mode === "rules" && (
                        <span className="flex-none rounded bg-gp-line px-1.5 py-px text-gp-text-2" title="Без ключа ИИ она понимает команды из подсказок">
                          базовый режим
                        </span>
                      )}
                      {notice && <span className="min-w-0">{notice}</span>}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {busy && (
          <div className="mt-[18px] flex w-fit items-center gap-2 text-sm text-gp-text-2">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            {undoing ? "Отменяю…" : generating ? "Генерирую план…" : phase === "acting" ? "Выполняю…" : "Юна думает…"}
          </div>
        )}
      </div>

      <div className="flex-none border-t border-gp-line px-5 pb-5 pt-3">
        {chips.length > 0 && (
          <>
            <p className="mb-1 text-[13px] text-gp-text-2">
              {contextOn ? "Про выделенное и не только" : entries.length === 0 ? "Можно спросить" : "Ещё можно"}
            </p>
            <div className="-mx-3 mb-3">
              {chips.slice(0, MAX_QUESTION_ROWS).map((text) => (
                <QuestionRow key={text} text={text} disabled={busy} onClick={() => void send(text)} />
              ))}
            </div>
          </>
        )}
        <div className="rounded-[10px] border border-gp-border bg-gp-field focus-within:border-gp-accent-soft-border">
          {contextOn && (
            // Метка-контекст: что Юна услышит как «выделенное». Крестик снимает
            // её — тогда вопрос уходит без выделения (см. sentSelection).
            <div className="flex items-center gap-2 px-2.5 pt-2">
              <span className="inline-flex h-[26px] max-w-full items-center gap-1.5 rounded-md bg-gp-selection-soft pl-2 pr-1.5 text-xs text-gp-selection-text">
                <span className="h-[7px] w-[7px] flex-none rounded-full bg-gp-selection" aria-hidden />
                <span className="truncate">{selectionLabel ?? "Выделено"}</span>
                <button
                  onClick={() => setDismissedContext(selectedIds)}
                  className="flex h-[18px] w-[18px] flex-none items-center justify-center rounded text-gp-selection hover:bg-white/10"
                  aria-label="Убрать контекст: спросить без выделения"
                >
                  <X className="h-3 w-3" strokeWidth={2} aria-hidden />
                </button>
              </span>
            </div>
          )}
          <div className="flex items-end gap-2 py-1.5 pl-3 pr-1.5">
            <label htmlFor="yuna-input" className="sr-only">
              Вопрос Юне
            </label>
            <textarea
              id="yuna-input"
              ref={inputRef}
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onInput={(e) => {
                // Растёт вместе с текстом, но не выше четырёх строк.
                const el = e.currentTarget;
                el.style.height = "auto";
                el.style.height = `${Math.min(el.scrollHeight, 22 * 4 + 14)}px`;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                } else if (e.key === "Escape") {
                  onClose();
                }
              }}
              disabled={!canTalk}
              placeholder={
                projectLost ? "Загрузите чертёж заново" : !projectId ? "Сначала загрузите проект" : contextOn && selectedItemId ? "Спросите про эту посадку" : "Напишите Юне…"
              }
              className="min-w-0 flex-1 resize-none border-0 bg-transparent py-[7px] text-[15px] leading-[22px] text-gp-text outline-none placeholder:text-gp-text-4 disabled:opacity-50"
            />
            <button
              onClick={() => void send()}
              disabled={!canTalk || !draft.trim() || busy}
              className="flex h-[34px] w-[34px] flex-none items-center justify-center rounded-lg bg-gp-accent text-gp-on-accent hover:bg-gp-accent-hover disabled:opacity-40"
              aria-label="Отправить"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M8 13V3" />
                <path d="M3.5 7.5L8 3l4.5 4.5" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </RightColumn>
  );
}
