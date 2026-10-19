"use client";

import { useEffect, type RefObject } from "react";

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/** Поведение модального окна: при открытии фокус уходит внутрь, Tab не
 * выбирается за его пределы (фокус-ловушка), Esc закрывает, а по закрытии
 * фокус возвращается туда, откуда окно открыли. Без этого клавиатурный
 * пользователь оставался бы «за» затемнением, а горячие клавиши карты (1, 2,
 * Del) продолжали бы работать через открытый диалог.
 *
 * `initialFocus` — какой элемент взять в фокус сразу (по умолчанию первый
 * доступный внутри). */
export function useDialog(ref: RefObject<HTMLElement>, onClose: () => void, initialFocus?: RefObject<HTMLElement>) {
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const root = ref.current;
    (initialFocus?.current ?? root?.querySelector<HTMLElement>(FOCUSABLE) ?? root)?.focus({ preventScroll: true });

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !root) return;
      const items = Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (items.length === 0) {
        e.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    }
    // capture: Esc закрывает диалог раньше, чем его увидят глобальные хоткеи карты.
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      previous?.focus?.({ preventScroll: true });
    };
    // onClose меняется на каждый рендер родителя, а ловушку не пересоздаём
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
