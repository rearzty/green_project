"use client";

import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";

import { normalizeQuery, searchCommands, type Command } from "@/lib/commands";
import { cn } from "@/lib/utils";

function Highlighted({ text, ranges }: { text: string; ranges: [number, number][] }) {
  if (ranges.length === 0) return <>{text}</>;
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  ranges.forEach(([start, end], i) => {
    if (start > cursor) parts.push(<Fragment key={`t${i}`}>{text.slice(cursor, start)}</Fragment>);
    parts.push(
      <strong key={`m${i}`} className="font-semibold">
        {text.slice(start, end)}
      </strong>
    );
    cursor = end;
  });
  if (cursor < text.length) parts.push(<Fragment key="tail">{text.slice(cursor)}</Fragment>);
  return <>{parts}</>;
}

type Row = { kind: "command"; id: string; command: Command; ranges: [number, number][] } | { kind: "ask"; id: string; text: string; typed: boolean };

const MAX_COMMANDS_EMPTY = 8;
const MAX_COMMANDS = 8;

export interface CommandPaletteProps {
  commands: Command[];
  /** Готовые вопросы Юне (те же, что подсказки в чате) — строки «Спросить Юну». */
  questions: string[];
  /** Спросить Юну: закрывает палитру, открывает её колонку и отправляет текст. */
  onAsk: (text: string) => void;
  onClose: () => void;
}

/** Палитра команд Ctrl+K (CommandK.dc.html, п. 9б): строка поиска над
 * затемнённой картой, группы «Действия» и «Спросить Юну». ↑↓ — выбрать, Enter —
 * выполнить, Tab — отправить написанное Юне, Esc — закрыть. По aria — combobox с
 * listbox: активная строка объявляется через aria-activedescendant, фокус всё
 * время в поле ввода. */
export function CommandPalette({ commands, questions, onAsk, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null;
    inputRef.current?.focus();
    return () => previousFocus.current?.focus?.({ preventScroll: true });
  }, []);

  const typed = query.trim();
  const { rows, firstAsk } = useMemo(() => {
    const matches = searchCommands(commands, query).slice(0, typed ? MAX_COMMANDS : MAX_COMMANDS_EMPTY);
    const commandRows: Row[] = matches.map((m) => ({ kind: "command", id: `cmd-${m.command.id}`, command: m.command, ranges: m.ranges }));
    // Вопросы Юне: написанное как есть (если запрос не пуст) и подходящие по
    // словам готовые вопросы; без запроса — первые два готовых.
    const tokens = normalizeQuery(query).split(/\s+/).filter(Boolean);
    const suggested = questions.filter((q) => tokens.length === 0 || tokens.every((t) => normalizeQuery(q).includes(t))).slice(0, 2);
    const askRows: Row[] = [];
    if (typed) askRows.push({ kind: "ask", id: "ask-typed", text: typed, typed: true });
    suggested.filter((q) => q !== typed).forEach((q, i) => askRows.push({ kind: "ask", id: `ask-${i}`, text: q, typed: false }));
    return { rows: [...commandRows, ...askRows], firstAsk: commandRows.length };
  }, [commands, questions, query, typed]);

  // Список поменялся — выбор на первую строку, иначе индекс мог выйти за край.
  useEffect(() => setActive(0), [query]);

  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-row="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function runRow(row: Row | undefined) {
    if (!row) return;
    // Закрываем до выполнения: действие может открыть диалог или увести фокус.
    onClose();
    if (row.kind === "ask") onAsk(row.text);
    else row.command.run();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => (rows.length === 0 ? 0 : (i + 1) % rows.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => (rows.length === 0 ? 0 : (i - 1 + rows.length) % rows.length));
    } else if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      runRow(rows[active]);
    } else if (e.key === "Tab") {
      // Tab — «спросить Юну»: написанное, а если поле пусто — выбранный вопрос.
      e.preventDefault();
      const row = rows[active];
      if (typed) {
        onClose();
        onAsk(typed);
      } else if (row?.kind === "ask") {
        runRow(row);
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
  }

  const activeId = rows[active]?.id;
  const renderRow = (row: Row, index: number) => {
    const selected = index === active;
    const Icon = row.kind === "command" ? row.command.icon : undefined;
    return (
      <button
        key={row.id}
        id={row.id}
        role="option"
        aria-selected={selected}
        data-row={index}
        tabIndex={-1}
        onMouseEnter={() => setActive(index)}
        onClick={() => runRow(row)}
        className={cn(
          "flex h-[42px] w-full items-center gap-3 px-4 text-left text-sm text-gp-text",
          selected && "bg-gp-hover-strong shadow-[inset_2px_0_0_#B5E07A]"
        )}
      >
        {row.kind === "command" ? (
          <>
            {row.command.dot ? (
              <span className="h-2 w-2 flex-none rounded-full bg-gp-violation" aria-hidden />
            ) : Icon ? (
              <Icon className="h-4 w-4 flex-none text-gp-text-2" aria-hidden />
            ) : (
              <span className="w-4 flex-none" />
            )}
            <span className="min-w-0 flex-1 truncate">
              <Highlighted text={row.command.title} ranges={row.ranges} />
            </span>
            {row.command.meta && <span className="flex-none font-mono text-xs text-gp-text-3">{row.command.meta}</span>}
          </>
        ) : (
          <>
            <span className="flex h-5 w-5 flex-none items-center justify-center rounded-full bg-[#3A4A2C] text-[11px] font-semibold text-gp-accent-text">Ю</span>
            <span className="min-w-0 flex-1 truncate text-gp-text-2">«{row.text}»</span>
            {selected && <span className="flex-none font-mono text-xs text-gp-text-3">{row.typed ? "Tab / Enter" : "Enter"}</span>}
          </>
        )}
      </button>
    );
  };

  return (
    <div className="fixed inset-0 z-[1250] flex items-start justify-center bg-[rgba(16,19,17,0.55)] px-4 pt-14 sm:pt-20" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Команды"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-[600px] overflow-hidden rounded-xl border border-gp-border bg-gp-panel shadow-[0_24px_60px_rgba(0,0,0,0.5)]"
      >
        <div className="flex h-14 items-center gap-3 border-b border-gp-line px-4">
          <Search className="h-4 w-4 flex-none text-gp-text-3" aria-hidden />
          <label htmlFor="command-input" className="sr-only">
            Команда или вопрос
          </label>
          <input
            id="command-input"
            ref={inputRef}
            type="text"
            role="combobox"
            aria-expanded
            aria-controls="command-list"
            aria-activedescendant={activeId}
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Команда или вопрос Юне"
            className="min-w-0 flex-1 border-0 bg-transparent text-[17px] text-gp-text outline-none placeholder:text-gp-text-4"
          />
          <span className="font-mono text-xs text-gp-text-3">Esc</span>
        </div>

        <div id="command-list" ref={listRef} role="listbox" aria-label="Результаты" className="max-h-[min(60vh,28rem)] overflow-y-auto pb-1">
          {rows.length === 0 && <p className="px-4 py-6 text-sm text-gp-text-3">Ничего не найдено — Tab отправит это Юне.</p>}
          {firstAsk > 0 && (
            <div role="group" aria-label="Действия">
              <p className="px-4 pb-1 pt-2.5 text-xs text-gp-text-3">Действия</p>
              {rows.slice(0, firstAsk).map((row, i) => renderRow(row, i))}
            </div>
          )}
          {rows.length > firstAsk && (
            <div role="group" aria-label="Спросить Юну">
              <p className="px-4 pb-1 pt-2.5 text-xs text-gp-text-3">Спросить Юну</p>
              {rows.slice(firstAsk).map((row, i) => renderRow(row, firstAsk + i))}
            </div>
          )}
        </div>

        <div className="mt-1 flex flex-wrap gap-x-[18px] gap-y-1 border-t border-gp-line px-4 py-2.5 text-xs text-gp-text-3">
          <span>
            <kbd className="font-mono text-gp-text-2">↑↓</kbd> выбрать
          </span>
          <span>
            <kbd className="font-mono text-gp-text-2">Enter</kbd> выполнить
          </span>
          <span>
            <kbd className="font-mono text-gp-text-2">Tab</kbd> спросить Юну
          </span>
        </div>
      </div>
    </div>
  );
}
