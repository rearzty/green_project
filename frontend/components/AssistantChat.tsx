"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2, Send, X } from "lucide-react";

import { askAssistant, type AssistantAction, type AssistantChatMessage, type AssistantGenerationSettings } from "@/lib/api";
import { errorMessage } from "@/lib/toast";
import { cn } from "@/lib/utils";

interface DisplayMessage {
  role: "user" | "assistant";
  content: string;
  isError?: boolean;
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
  onApplyAction: (action: AssistantAction) => Promise<void>;
}

/** Floating chat widget, bottom-left (Toaster owns bottom-right -- see
 * CLAUDE.md's z-index registry for the rest of the floating UI). Messages
 * live in plain component state on purpose: "remembered while the tab stays
 * open, forgotten on reload" is exactly what useState already gives for
 * free -- no sessionStorage, no backend persistence, matching the backend
 * being a stateless proxy (assistant_service.py's own docstring explains why). */
export function AssistantChat({ projectId, generating, settings, onApplyAction }: AssistantChatProps) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<DisplayMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  // /yuna-avatar.png isn't checked into the repo (it's the user's own art,
  // dropped into frontend/public/ by hand) -- falls back to a plain "Ю"
  // initial via onError so the widget works identically before and after
  // that file shows up, no code change needed either way.
  const [avatarError, setAvatarError] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  function avatar(className: string) {
    if (avatarError) {
      return <span className={cn(className, "flex items-center justify-center rounded-full bg-greenery-700 font-semibold text-white")}>Ю</span>;
    }
    return <img src="/yuna-avatar.png" alt="Юна" className={cn(className, "rounded-full object-cover")} onError={() => setAvatarError(true)} />;
  }

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, open]);

  const busy = sending || generating;

  async function send() {
    const text = draft.trim();
    if (!text || !projectId || busy) return;
    setDraft("");
    // The backend holds no conversation state of its own (see
    // assistant_service.py) -- the full transcript rides along on every
    // message, built from what's already on screen rather than a second
    // copy kept in sync separately.
    const history: AssistantChatMessage[] = messages.filter((m) => !m.isError).map(({ role, content }) => ({ role, content }));
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setSending(true);
    try {
      const result = await askAssistant(projectId, text, history, settings);
      setMessages((prev) => [...prev, { role: "assistant", content: result.reply }]);
      if (result.action) {
        try {
          await onApplyAction(result.action);
        } catch (e) {
          setMessages((prev) => [...prev, { role: "assistant", content: errorMessage(e), isError: true }]);
        }
      }
    } catch (e) {
      setMessages((prev) => [...prev, { role: "assistant", content: errorMessage(e), isError: true }]);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="fixed bottom-4 left-4 z-[1100] flex flex-col items-start gap-2">
      {open && (
        <div className="flex h-[28rem] w-80 max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-lg border border-stone-700 bg-stone-900 shadow-lg">
          <div className="flex items-center justify-between gap-2 border-b border-stone-700 bg-stone-800/60 px-3 py-2">
            <div className="flex items-center gap-2">
              {avatar("h-7 w-7 flex-none text-sm")}
              <div>
                <p className="text-sm font-medium text-stone-50">Юна</p>
                <p className="text-[11px] text-stone-400">Помощник по плану озеленения</p>
              </div>
            </div>
            <button onClick={() => setOpen(false)} className="rounded p-1 text-stone-400 hover:bg-stone-800 hover:text-stone-200" aria-label="Закрыть чат">
              <X className="h-4 w-4" aria-hidden />
            </button>
          </div>

          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-2">
            {messages.length === 0 && (
              <p className="mt-2 text-xs text-stone-400">
                Привет! Помогу настроить план — например «побольше деревьев» или «посади кусты пореже».
              </p>
            )}
            <div className="flex flex-col gap-2">
              {messages.map((m, i) => (
                <div
                  key={i}
                  className={cn(
                    "max-w-[85%] whitespace-pre-wrap rounded-lg px-2.5 py-1.5 text-sm",
                    m.role === "user" ? "self-end bg-greenery-700 text-white" : "self-start bg-stone-800 text-stone-100",
                    m.isError && "self-start border border-red-900 bg-red-950/60 text-red-300"
                  )}
                >
                  {m.content}
                </div>
              ))}
              {busy && (
                <div className="flex items-center gap-1.5 self-start rounded-lg bg-stone-800 px-2.5 py-1.5 text-xs text-stone-400">
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                  {generating ? "Генерирую план…" : "Юна думает…"}
                </div>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 border-t border-stone-700 p-2">
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              disabled={!projectId || busy}
              placeholder={projectId ? "Напишите Юне…" : "Сначала загрузите проект"}
              className="flex-1 rounded border border-stone-600 bg-stone-900 px-2 py-1.5 text-sm disabled:opacity-50"
            />
            <button
              onClick={send}
              disabled={!projectId || !draft.trim() || busy}
              className="flex-none rounded-md bg-greenery-600 p-1.5 text-white hover:bg-greenery-700 disabled:opacity-40"
              aria-label="Отправить"
            >
              <Send className="h-4 w-4" aria-hidden />
            </button>
          </div>
        </div>
      )}

      <button
        onClick={() => setOpen((v) => !v)}
        className="flex h-12 w-12 items-center justify-center overflow-hidden rounded-full bg-greenery-600 text-white shadow-lg hover:bg-greenery-700"
        aria-pressed={open}
        title="Юна — помощник по плану"
      >
        {open ? <X className="h-5 w-5" aria-hidden /> : avatar("h-12 w-12 text-base")}
      </button>
    </div>
  );
}
