"use client";

import { useSyncExternalStore } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

import { dismissToast, getServerToasts, getToasts, holdToast, releaseToast, subscribeToasts, type ToastKind } from "@/lib/toast";

const KIND_STYLES: Record<ToastKind, { accent: string; icon: typeof Info; iconClass: string; title: string }> = {
  error: { accent: "border-l-red-500", icon: AlertTriangle, iconClass: "text-red-600", title: "Не получилось" },
  success: { accent: "border-l-greenery-500", icon: CheckCircle2, iconClass: "text-greenery-600", title: "Готово" },
  info: { accent: "border-l-sky-500", icon: Info, iconClass: "text-sky-600", title: "Обратите внимание" },
};

/** Stacked notification cards, bottom-right over the map. z-index sits above
 * Leaflet's own controls (1000) so a toast is never hidden under the zoom
 * buttons or attribution. */
export function Toaster() {
  const toasts = useSyncExternalStore(subscribeToasts, getToasts, getServerToasts);

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[1100] flex w-[22rem] max-w-[calc(100vw-2rem)] flex-col gap-2" aria-live="polite">
      {toasts.map((t) => {
        const style = KIND_STYLES[t.kind];
        const Icon = style.icon;
        return (
          <div
            key={t.id}
            role={t.kind === "error" ? "alert" : "status"}
            onMouseEnter={() => holdToast(t.id)}
            onMouseLeave={() => releaseToast(t.id)}
            className={`gp-toast pointer-events-auto flex items-start gap-3 rounded-lg border border-l-4 border-stone-200 bg-white/95 p-3 shadow-lg backdrop-blur ${style.accent}`}
          >
            <Icon className={`mt-0.5 h-5 w-5 flex-none ${style.iconClass}`} aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-stone-900">{style.title}</p>
              <p className="mt-0.5 break-words text-sm text-stone-600">{t.message}</p>
              {t.action && (
                <button
                  onClick={() => {
                    t.action?.onClick();
                    dismissToast(t.id);
                  }}
                  className="mt-2 rounded-md border border-greenery-300 px-2 py-1 text-xs font-medium text-greenery-700 hover:bg-greenery-50"
                >
                  {t.action.label}
                </button>
              )}
            </div>
            <button
              onClick={() => dismissToast(t.id)}
              className="flex-none rounded p-0.5 text-stone-400 hover:bg-stone-100 hover:text-stone-600"
              aria-label="Закрыть уведомление"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
