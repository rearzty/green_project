"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, Eraser, Loader2, Send, X } from "lucide-react";

import {
  askAssistant,
  getAssistantStatus,
  type AssistantChatMessage,
  type AssistantCommand,
  type AssistantGenerationSettings,
  type AssistantMessageResult,
  type AssistantSelection,
  type AssistantStatus,
} from "@/lib/api";
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
  /** null — план ещё не проверен на нормативы. */
  violationsCount: number | null;
  view: "2d" | "3d";
  /** Деревьев в плане на экране — для подсказки «Выдели все деревья». */
  planTrees: number;
  /** Открытая боковая панель занимает левый край — кнопка чата встаёт правее
   * неё, иначе на десктопе перекрывала переключатель «Эвристика / ML». */
  sidebarOpen?: boolean;
  /** Выполнить команды ответа по порядку (app/page.tsx); не бросает —
   * ошибка команды приходит её строкой с ok: false. */
  onCommands: (commands: AssistantCommand[]) => Promise<CommandResult[]>;
}

/** Сколько последних реплик уходит Юне историей: запрос не растёт вместе с
 * разговором, а для «сделай их кустами» хватает и пары последних. */
const HISTORY_LIMIT = 20;
const NO_ENTRIES: ChatEntry[] = [];
const CHIPS_FADE = "linear-gradient(to right, black calc(100% - 2rem), transparent)";

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
          <strong key={i} className="font-semibold text-stone-50">
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
        className="min-w-0 truncate rounded-full bg-greenery-700/30 px-1.5 py-px text-[10px] font-medium text-greenery-300"
        title={`Отвечает языковая модель ${status.model ?? status.provider}. Если она недоступна, Юна понимает команды из подсказок.`}
      >
        ИИ · {model}
      </span>
    );
  }
  return (
    <span
      className="flex-none rounded-full bg-stone-700/70 px-1.5 py-px text-[10px] text-stone-300"
      title="Без ключа ИИ она понимает команды из подсказок"
    >
      базовый режим
    </span>
  );
}

function Chip({ text, disabled, onClick }: { text: string; disabled: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="flex-none whitespace-nowrap rounded-full border border-stone-700 bg-stone-800/50 px-2.5 py-1 text-[11px] text-stone-300 transition-colors hover:border-greenery-600 hover:bg-stone-800 hover:text-stone-100 disabled:opacity-40"
    >
      {text}
    </button>
  );
}

/** Floating chat widget, bottom-left (Toaster owns bottom-right -- see
 * CLAUDE.md's z-index registry for the rest of the floating UI). Юна отвечает
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
  violationsCount,
  view,
  planTrees,
  sidebarOpen,
  onCommands,
}: AssistantChatProps) {
  const [open, setOpen] = useState(false);
  // Разговор вместе с проектом, к которому он относится, — одним значением:
  // иначе эффект сохранения успевал бы записать разговор старого проекта
  // под новый (та же ловушка, что у истории правок, см. CLAUDE.md про undo).
  const [chat, setChat] = useState<{ projectId?: string; entries: ChatEntry[] }>({ entries: NO_ENTRIES });
  const [draft, setDraft] = useState("");
  const [phase, setPhase] = useState<"idle" | "thinking" | "acting">("idle");
  const [undoing, setUndoing] = useState(false);
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [unread, setUnread] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  // /yuna-avatar.png isn't checked into the repo (it's the user's own art,
  // dropped into frontend/public/ by hand) -- falls back to a plain "Ю"
  // initial via onError so the widget works identically before and after
  // that file shows up, no code change needed either way.
  const [avatarError, setAvatarError] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
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

  useEffect(() => {
    // Только при открытии, не после каждого ответа: фокус в поле глушит
    // горячие клавиши карты (useKeyboardShortcuts пропускает текстовые поля),
    // а пока Юна думала, пользователь мог уйти править карту. И только с
    // мышью — на телефоне фокус сразу поднимал бы клавиатуру.
    if (open && window.matchMedia("(pointer: fine)").matches) inputRef.current?.focus();
  }, [open]);

  function appendEntry(forProject: string, entry: ChatEntry) {
    setChat((prev) => (prev.projectId === forProject ? { ...prev, entries: [...prev.entries, entry] } : prev));
    if (entry.role === "assistant" && !openRef.current) setUnread(true);
  }

  function patchEntry(forProject: string, id: string, patch: (entry: ChatEntry) => ChatEntry) {
    setChat((prev) =>
      prev.projectId === forProject ? { ...prev, entries: prev.entries.map((entry) => (entry.id === id ? patch(entry) : entry)) } : prev
    );
  }

  async function send(preset?: string) {
    const text = (preset ?? draft).trim();
    if (!text || !projectId || busy || sendingRef.current) return;
    sendingRef.current = true;
    if (preset === undefined) setDraft("");
    const forProject = projectId;
    // The backend holds no conversation state of its own (see
    // assistant_service.py) -- the transcript rides along on every message,
    // built from what's already on screen rather than a second copy.
    const history = toHistory(entries);
    appendEntry(forProject, { id: newEntryId(), role: "user", content: text });
    setPhase("thinking");
    try {
      let result: AssistantMessageResult;
      try {
        result = await askAssistant(forProject, text, history, settings, { planId, selectedItemId, selection, violationsCount, view });
      } catch (e) {
        appendEntry(forProject, { id: newEntryId(), role: "assistant", content: errorMessage(e), isError: true, retryText: text });
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
        results = await onCommands(commands);
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
      if (!openRef.current) setUnread(true);
    } finally {
      sendingRef.current = false;
      setPhase("idle");
    }
  }

  function retry(entry: ChatEntry) {
    const text = entry.retryText;
    if (!text || !projectId || busy) return;
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
    setLine({ undo: "running", undoError: undefined });
    setUndoing(true);
    try {
      await undo();
      undoFns.current.delete(key);
      setLine({ undo: "done" });
    } catch (e) {
      setLine({ undo: "available", undoError: errorMessage(e) });
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

  function toggleOpen() {
    setOpen((v) => !v);
    setUnread(false);
  }

  function avatar(className: string) {
    if (avatarError) {
      return <span className={cn(className, "flex items-center justify-center rounded-full bg-greenery-700 font-semibold text-white")}>Ю</span>;
    }
    return <img src="/yuna-avatar.png" alt="Юна" className={cn(className, "rounded-full object-cover")} onError={() => setAvatarError(true)} />;
  }

  const chips = suggestionChips({
    hasProject: Boolean(projectId),
    hasPlan: Boolean(planId),
    settings,
    planTrees,
    violationsCount,
    selection,
  });

  function renderResult(entry: ChatEntry, line: ResultLine, index: number) {
    const canUndo = undoFns.current.has(`${entry.id}:${index}`);
    return (
      <li key={index} className="flex flex-col gap-0.5">
        <div className="flex items-start gap-1.5 text-[11.5px] leading-snug">
          {line.ok ? (
            <Check className="mt-0.5 h-3 w-3 flex-none text-greenery-300" aria-label="Выполнено" />
          ) : (
            <AlertTriangle className="mt-0.5 h-3 w-3 flex-none text-amber-400" aria-label="Не выполнено" />
          )}
          <span className={cn("min-w-0 flex-1", line.ok ? "text-stone-300" : "text-amber-200/90", line.undo === "done" && "text-stone-500 line-through")}>
            {line.text}
          </span>
          {line.undo === "done" ? (
            <span className="flex-none text-stone-500">отменено</span>
          ) : canUndo && line.undo === "running" ? (
            <span className="flex-none text-stone-500">отменяю…</span>
          ) : canUndo && line.undo === "available" ? (
            <button
              onClick={() => runUndo(entry.id, index)}
              disabled={busy}
              className="flex-none font-medium text-greenery-300 underline-offset-2 hover:underline disabled:opacity-40"
            >
              Отменить
            </button>
          ) : null}
        </div>
        {line.undoError && <p className="pl-[18px] text-[11px] leading-snug text-amber-300/90">{line.undoError}</p>}
      </li>
    );
  }

  // Одно и то же пояснение (например, «ключ ИИ не задан») под каждым ответом
  // подряд — шум: показываем, только когда оно сменилось.
  let lastNotice: string | null = null;

  return (
    <div className={cn("fixed bottom-4 left-4 z-[1100] flex flex-col items-start gap-2", sidebarOpen && "md:left-[400px]")}>
      {open && (
        <div className="flex h-[32rem] max-h-[calc(100vh-6rem)] w-[22rem] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-xl border border-stone-700/80 bg-stone-900/95 text-stone-100 shadow-lg backdrop-blur">
          <div className="flex items-center justify-between gap-2 border-b border-stone-800 bg-stone-800/40 px-3 py-2">
            <div className="flex min-w-0 items-center gap-2">
              {avatar("h-8 w-8 flex-none text-sm")}
              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-1.5">
                  <p className="flex-none text-sm font-semibold text-stone-50">Юна</p>
                  {status && <ModeBadge status={status} />}
                </div>
                <p className="truncate text-[11px] text-stone-400">Помощник по плану озеленения</p>
              </div>
            </div>
            <div className="flex flex-none items-center gap-0.5">
              <button
                onClick={() => (confirmClear ? clearChat() : setConfirmClear(true))}
                onBlur={() => setConfirmClear(false)}
                disabled={entries.length === 0 || busy}
                title={confirmClear ? "Нажмите ещё раз — чат очистится" : "Очистить чат"}
                aria-label="Очистить чат"
                className={cn(
                  "rounded p-1 transition-colors disabled:opacity-30",
                  confirmClear ? "bg-red-900/80 text-red-200" : "text-stone-400 hover:bg-stone-800 hover:text-stone-200"
                )}
              >
                <Eraser className="h-4 w-4" aria-hidden />
              </button>
              <button onClick={toggleOpen} className="rounded p-1 text-stone-400 hover:bg-stone-800 hover:text-stone-200" aria-label="Закрыть чат">
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>
          </div>

          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3" aria-live="polite">
            {entries.length === 0 ? (
              <div className="flex flex-col gap-2.5">
                <p className="text-[13px] leading-relaxed text-stone-300">
                  Привет! Я Юна. Объясню план и поменяю его: перестрою с другими настройками, выделю и поправлю посадки, выгружу DXF
                  или отчёт обоснований.
                </p>
                <p className="text-xs leading-relaxed text-stone-500">
                  {!projectId
                    ? "Сначала загрузите чертёж в панели слева."
                    : status && !status.llm_configured
                      ? "Сейчас я в базовом режиме, без ИИ: понимаю команды вроде подсказок ниже."
                      : "Выделите посадку на карте — расскажу, почему она стоит именно там."}
                </p>
                {chips.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {chips.map((text) => (
                      <Chip key={text} text={text} disabled={busy} onClick={() => void send(text)} />
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-2.5">
                {entries.map((entry) => {
                  if (entry.role === "user") {
                    return (
                      <div
                        key={entry.id}
                        className="max-w-[85%] self-end whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-greenery-700 px-3 py-1.5 text-sm text-white"
                      >
                        {entry.content}
                      </div>
                    );
                  }
                  if (entry.isError) {
                    return (
                      <div
                        key={entry.id}
                        role="alert"
                        className="max-w-[92%] self-start rounded-2xl rounded-bl-md border border-red-900/70 bg-red-950/50 px-3 py-2 text-sm text-red-200"
                      >
                        <p className="flex gap-1.5">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-red-400" aria-hidden />
                          <span className="min-w-0 break-words">{entry.content}</span>
                        </p>
                        {entry.retryText && (
                          <button
                            onClick={() => retry(entry)}
                            disabled={busy}
                            className="mt-1 pl-5 text-xs font-medium text-red-300 underline-offset-2 hover:underline disabled:opacity-40"
                          >
                            Повторить
                          </button>
                        )}
                      </div>
                    );
                  }
                  const notice = entry.notice && entry.notice !== lastNotice ? entry.notice : null;
                  if (entry.notice) lastNotice = entry.notice;
                  return (
                    <div key={entry.id} className="flex max-w-[92%] flex-col gap-1 self-start">
                      <div className="whitespace-pre-wrap break-words rounded-2xl rounded-bl-md bg-stone-800 px-3 py-2 text-sm leading-relaxed text-stone-100">
                        <RichText text={entry.content} />
                      </div>
                      {entry.results && entry.results.length > 0 && (
                        <ul className="flex flex-col gap-1 px-1">{entry.results.map((line, i) => renderResult(entry, line, i))}</ul>
                      )}
                      {(entry.mode === "rules" || notice) && (
                        <p className="flex items-start gap-1.5 px-1 text-[10.5px] leading-snug text-stone-500">
                          {entry.mode === "rules" && (
                            <span className="flex-none rounded bg-stone-800 px-1 py-px text-stone-400" title="Без ключа ИИ она понимает команды из подсказок">
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
              <div className="mt-2.5 flex w-fit items-center gap-1.5 rounded-2xl bg-stone-800 px-3 py-1.5 text-xs text-stone-400">
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                {undoing ? "Отменяю…" : generating ? "Генерирую план…" : phase === "acting" ? "Выполняю…" : "Юна думает…"}
              </div>
            )}
          </div>

          {entries.length > 0 && chips.length > 0 && (
            // Одна строка с прокруткой вбок: подсказки не съедают место у
            // разговора. Колесо мыши крутит её по горизонтали, правый край
            // растворяется — видно, что дальше есть ещё.
            <div
              className="flex gap-1.5 overflow-x-auto border-t border-stone-800 pl-2 pr-8 pt-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
              style={{ maskImage: CHIPS_FADE, WebkitMaskImage: CHIPS_FADE }}
              onWheel={(e) => {
                if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) e.currentTarget.scrollLeft += e.deltaY;
              }}
            >
              {chips.map((text) => (
                <Chip key={text} text={text} disabled={busy} onClick={() => void send(text)} />
              ))}
            </div>
          )}

          <div className={cn("flex items-center gap-2 p-2", !(entries.length > 0 && chips.length > 0) && "border-t border-stone-800")}>
            <input
              ref={inputRef}
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send();
                } else if (e.key === "Escape") {
                  setOpen(false);
                }
              }}
              disabled={!projectId}
              placeholder={projectId ? "Напишите Юне…" : "Сначала загрузите проект"}
              className="min-w-0 flex-1 rounded-md border border-stone-700 bg-stone-950 px-2.5 py-1.5 text-sm text-stone-100 placeholder:text-stone-600 focus:border-greenery-600 focus:outline-none disabled:opacity-50"
            />
            <button
              onClick={() => void send()}
              disabled={!projectId || !draft.trim() || busy}
              className="flex-none rounded-md bg-greenery-600 p-1.5 text-white hover:bg-greenery-700 disabled:opacity-40"
              aria-label="Отправить"
            >
              <Send className="h-4 w-4" aria-hidden />
            </button>
          </div>
        </div>
      )}

      <div className="relative">
        <button
          onClick={toggleOpen}
          className="flex h-12 w-12 items-center justify-center overflow-hidden rounded-full bg-greenery-600 text-white shadow-lg hover:bg-greenery-700"
          aria-pressed={open}
          aria-label={open ? "Закрыть чат с Юной" : "Открыть чат с Юной"}
          title="Юна — помощник по плану"
        >
          {open ? <X className="h-5 w-5" aria-hidden /> : avatar("h-12 w-12 text-base")}
        </button>
        {unread && !open && (
          <span className="pointer-events-none absolute -right-0.5 -top-0.5 h-3.5 w-3.5 rounded-full border-2 border-stone-950 bg-amber-400" aria-hidden />
        )}
      </div>
    </div>
  );
}
