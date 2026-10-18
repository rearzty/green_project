"use client";

import { useSyncExternalStore } from "react";
import { X } from "lucide-react";

import { dismissToast, getServerToasts, getToasts, holdToast, releaseToast, subscribeToasts, type ToastKind } from "@/lib/toast";

const TITLES: Record<ToastKind, string> = {
  error: "Не получилось",
  success: "Готово",
  info: "Обратите внимание",
};

/** Stacked notification cards, bottom-right over the map. z-index sits above
 * Leaflet's own controls (1000) so a toast is never hidden under the zoom
 * buttons or attribution -- and above the phone drawer (app/page.tsx,
 * 1120): an upload error raised from inside the open panel must not land
 * underneath it. Графитовый стиль: фон панели, рамка, без цветной полосы
 * слева — кроме ошибки, у которой 3px-полоса цвета нарушения (внутренняя
 * тень, чтобы не двигать вёрстку). Кнопка действия — текст акцентом. */
export function Toaster() {
  const toasts = useSyncExternalStore(subscribeToasts, getToasts, getServerToasts);

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[1150] flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => (
        <div
          key={t.id}
          role={t.kind === "error" ? "alert" : "status"}
          onMouseEnter={() => holdToast(t.id)}
          onMouseLeave={() => releaseToast(t.id)}
          className={`gp-toast pointer-events-auto flex items-start gap-3 rounded-lg border border-gp-border bg-gp-panel p-3.5 ${
            t.kind === "error" ? "shadow-[inset_3px_0_0_#FF9351,0_20px_25px_-5px_rgba(0,0,0,.4)]" : "shadow-xl"
          }`}
        >
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-gp-text">{TITLES[t.kind]}</p>
            <p className="mt-0.5 break-words text-sm leading-5 text-gp-text-2">{t.message}</p>
            {t.action && (
              <button
                onClick={() => {
                  t.action?.onClick();
                  dismissToast(t.id);
                }}
                className="mt-2 h-8 rounded-md text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover"
              >
                {t.action.label}
              </button>
            )}
          </div>
          <button
            onClick={() => dismissToast(t.id)}
            className="flex h-7 w-7 flex-none items-center justify-center rounded-md text-gp-text-3 hover:bg-gp-hover hover:text-gp-text"
            aria-label="Закрыть уведомление"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}
