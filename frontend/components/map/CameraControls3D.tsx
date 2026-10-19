"use client";

import type { CameraPose, WalkState } from "@/components/ThreeDView";
import { cn } from "@/lib/utils";

export interface CameraControls3DProps {
  onPose: (pose: CameraPose) => void;
  walk: WalkState;
  autoRotate: boolean;
  onToggleAutoRotate: () => void;
}

/** Нижняя середина 3D-вида. «Общий» и «Сверху» — чтобы увидеть участок целиком, а
 * ‹ › — чтобы рассмотреть посадки вблизи: камера подлетает к каждому участку улицы
 * (~40 м) по очереди. Издалека на километровой улице дерево — точка, и осмотр с
 * общего вида ничего не даёт; руками же вести камеру вдоль всей улицы долго. */
export function CameraControls3D({ onPose, walk, autoRotate, onToggleAutoRotate }: CameraControls3DProps) {
  const button = "h-[30px] rounded-md px-3 text-[13px] font-medium text-gp-text-2 transition-colors hover:text-gp-text disabled:opacity-40 disabled:hover:text-gp-text-2";
  const arrow = "h-[30px] w-[30px] rounded-md text-base font-medium text-gp-text-2 transition-colors hover:text-gp-text disabled:opacity-40 disabled:hover:text-gp-text-2";
  const hasStops = walk.total > 0;
  return (
    <div data-map-overlay className="pointer-events-none absolute bottom-3 left-1/2 z-[1050] flex -translate-x-1/2 flex-col items-center gap-1.5">
      <p className="m-0 rounded-md bg-gp-panel/80 px-2 py-0.5 text-xs text-gp-text-3">Двойной щелчок — подлететь · колесо приближает к курсору · ← → — по участкам</p>
      <div role="group" aria-label="Ракурс камеры" className="pointer-events-auto flex items-center gap-0.5 rounded-lg border border-gp-border bg-gp-panel p-[3px] shadow-lg">
        <button onClick={() => onPose("overview")} title="Общий вид участка" className={button}>
          Общий
        </button>
        <button onClick={() => onPose("top")} title="Вид строго сверху" className={button}>
          Сверху
        </button>
        <span className="mx-1 h-5 w-px bg-gp-border" aria-hidden />
        <button onClick={() => onPose("walk-prev")} disabled={!hasStops} aria-label="Предыдущий участок" title="Предыдущий участок посадок" className={arrow}>
          ‹
        </button>
        <span className="min-w-[96px] text-center text-[13px] font-medium text-gp-text-2" aria-live="polite">
          {!hasStops ? "Обход" : walk.index > 0 ? `Участок ${walk.index} из ${walk.total}` : `Обход · ${walk.total}`}
        </span>
        <button onClick={() => onPose("walk-next")} disabled={!hasStops} aria-label="Следующий участок" title="Следующий участок посадок" className={arrow}>
          ›
        </button>
        <span className="mx-1 h-5 w-px bg-gp-border" aria-hidden />
        <button onClick={onToggleAutoRotate} aria-pressed={autoRotate} title="Медленный облёт вокруг участка" className={cn(button, autoRotate && "bg-gp-border text-gp-text")}>
          Облёт
        </button>
      </div>
    </div>
  );
}
