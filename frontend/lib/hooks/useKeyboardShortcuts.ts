"use client";

import { useEffect, useRef } from "react";

export interface KeyboardActions {
  undo: () => void;
  redo: () => void;
  deleteSelection: () => void;
  retype: (type: "tree" | "shrub") => void;
  toggleSelectMode: () => void;
  selectAll: () => void;
  escape: () => void;
  hasPlan: boolean;
  hasSelection: boolean;
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

/** Global keyboard shortcuts for plan editing (Ctrl+Z/Y, S, Del, 1/2, Esc,
 * Ctrl+A) -- registers one `keydown` listener for the component's lifetime
 * and reads the latest callbacks through a ref, so `actions` (which closes
 * over component state and is a new object every render) doesn't force the
 * listener to be torn down and re-added on every render either. */
export function useKeyboardShortcuts(actions: KeyboardActions) {
  const actionsRef = useRef(actions);
  actionsRef.current = actions;

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.repeat || isTextInput(e.target)) return;
      const a = actionsRef.current;
      const code = physicalKey(e);
      const mod = e.ctrlKey || e.metaKey;
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
