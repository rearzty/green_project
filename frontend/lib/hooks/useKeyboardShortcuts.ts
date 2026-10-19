"use client";

import { useEffect, useRef } from "react";

export interface KeyboardActions {
  undo: () => void;
  redo: () => void;
  deleteSelection: () => void;
  retype: (type: "tree" | "shrub") => void;
  toggleSelectMode: () => void;
  /** V — инструмент «Выбор»: обычный режим (карта двигается, клик выделяет). */
  selectTool: () => void;
  selectAll: () => void;
  escape: () => void;
  /** Ctrl/Cmd+K — палитра команд; работает и в текстовых полях. */
  openPalette: () => void;
  /** N — к следующему нарушению. */
  nextViolation: () => void;
  hasPlan: boolean;
  hasSelection: boolean;
  /** Открыт диалог или палитра: хоткеи карты (1, 2, Del, S…) молчат, иначе
   * клавиша, нажатая «поверх» окна, правила бы план под ним. Ctrl+K работает всегда. */
  suspended?: boolean;
}

function isTextInput(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable || target.tagName === "TEXTAREA" || target.tagName === "SELECT") return true;
  return target.tagName === "INPUT" && !["checkbox", "radio", "button", "submit", "file"].includes((target as HTMLInputElement).type);
}

// e.code (physical key), not e.key: on a Russian layout e.key for Z/Y/S/A
// arrives as "я"/"н"/"ы"/"ф". Some sources send no e.code at all though
// (on-screen/mobile keyboards, remote-desktop and automation tools) -- for
// those, map e.key back from either layout instead.
const KEY_TO_CODE: Record<string, string> = {
  z: "KeyZ",
  я: "KeyZ",
  y: "KeyY",
  н: "KeyY",
  s: "KeyS",
  ы: "KeyS",
  v: "KeyV",
  м: "KeyV",
  k: "KeyK",
  л: "KeyK",
  n: "KeyN",
  т: "KeyN",
  a: "KeyA",
  ф: "KeyA",
  "1": "Digit1",
  "!": "Digit1",
  "2": "Digit2",
  "@": "Digit2",
  '"': "Digit2",
  delete: "Delete",
  backspace: "Backspace",
  escape: "Escape",
  esc: "Escape",
};

function physicalKey(e: KeyboardEvent): string {
  return e.code || KEY_TO_CODE[e.key.toLowerCase()] || e.key;
}

/** Global keyboard shortcuts for plan editing (Ctrl+Z/Y, V, S, N, Del, 1/2, Esc,
 * Ctrl+A, Ctrl+K) -- registers one `keydown` listener for the component's lifetime
 * and reads the latest callbacks through a ref, so `actions` (which closes
 * over component state and is a new object every render) doesn't force the
 * listener to be torn down and re-added on every render either. */
export function useKeyboardShortcuts(actions: KeyboardActions) {
  const actionsRef = useRef(actions);
  actionsRef.current = actions;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.repeat) return;
      const a = actionsRef.current;
      const code = physicalKey(e);
      const mod = e.ctrlKey || e.metaKey;
      // Палитра команд — единственное, что работает и в поле ввода (Юна,
      // название проекта), и поверх открытого окна.
      if (mod && !e.altKey && !e.shiftKey && code === "KeyK") {
        e.preventDefault();
        a.openPalette();
        return;
      }
      if (isTextInput(e.target) || a.suspended) return;
      if (mod && !e.altKey) {
        if (code === "KeyZ") {
          e.preventDefault();
          if (e.shiftKey) a.redo();
          else a.undo();
        } else if (code === "KeyY") {
          e.preventDefault();
          a.redo();
        } else if (code === "KeyA" && a.hasPlan) {
          e.preventDefault();
          a.selectAll();
        }
        return;
      }
      if (mod || e.altKey) return;
      switch (code) {
        case "KeyS":
          // No hasPlan guard here: toggleSelectMode() itself decides whether
          // turning on is allowed, but always allows turning back off.
          e.preventDefault();
          a.toggleSelectMode();
          break;
        case "KeyN":
          if (!a.hasPlan) return;
          e.preventDefault();
          a.nextViolation();
          break;
        case "KeyV":
          // Всегда можно: «Выбор» — обычный режим, выйти в него разрешено и без плана.
          e.preventDefault();
          a.selectTool();
          break;
        case "Delete":
        case "Backspace":
          if (!a.hasSelection) return;
          e.preventDefault();
          a.deleteSelection();
          break;
        case "Digit1":
        case "Numpad1":
          if (!a.hasSelection) return;
          e.preventDefault();
          a.retype("tree");
          break;
        case "Digit2":
        case "Numpad2":
          if (!a.hasSelection) return;
          e.preventDefault();
          a.retype("shrub");
          break;
        case "Escape":
          a.escape();
          break;
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
}
