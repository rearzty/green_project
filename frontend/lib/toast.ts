/** Tiny module-level toast store -- callable from anywhere (React handlers,
 * Leaflet DOM callbacks, lib code) without threading a context through, and
 * rendered by a single <Toaster/> (components/Toaster.tsx). Replaces the
 * browser's native alert(), which blocked the page and looked nothing like
 * the rest of the UI. */

export type ToastKind = "error" | "success" | "info";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  action?: ToastAction;
}

const DURATION_MS: Record<ToastKind, number> = { error: 7000, success: 4500, info: 4500 };
/** A toast with a button (e.g. "Отменить" after a delete) needs time to be read and reached. */
const ACTION_DURATION_MS = 9000;
const MAX_VISIBLE = 4;

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();
const timers = new Map<number, ReturnType<typeof setTimeout>>();

function emit() {
  listeners.forEach((listener) => listener());
}

export function dismissToast(id: number) {
  clearTimeout(timers.get(id));
  timers.delete(id);
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

function scheduleDismiss(toast: Toast) {
  clearTimeout(timers.get(toast.id));
  timers.set(
    toast.id,
    setTimeout(() => dismissToast(toast.id), toast.action ? ACTION_DURATION_MS : DURATION_MS[toast.kind])
  );
}

/** Hovering a toast holds it on screen; leaving restarts its full timer. */
export function holdToast(id: number) {
  clearTimeout(timers.get(id));
  timers.delete(id);
}

export function releaseToast(id: number) {
  const toast = toasts.find((t) => t.id === id);
  if (toast) scheduleDismiss(toast);
}

function show(kind: ToastKind, message: string, action?: ToastAction): number {
  // The same message again while it's still on screen (e.g. repeatedly
  // dropping an item outside the territory) just restarts its timer instead
  // of stacking identical cards.
  const existing = toasts.find((t) => t.kind === kind && t.message === message && !t.action && !action);
  if (existing) {
    scheduleDismiss(existing);
    return existing.id;
  }

  const toast: Toast = { id: nextId++, kind, message, action };
  toasts = [...toasts, toast];
  while (toasts.length > MAX_VISIBLE) dismissToast(toasts[0].id);
  scheduleDismiss(toast);
  emit();
  return toast.id;
}

export const toast = {
  error: (message: string, action?: ToastAction) => show("error", message, action),
  success: (message: string, action?: ToastAction) => show("success", message, action),
  info: (message: string, action?: ToastAction) => show("info", message, action),
};

/** For catch blocks: whatever was thrown, as a user-facing message. */
export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function subscribeToasts(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getToasts(): Toast[] {
  return toasts;
}

const NO_TOASTS: Toast[] = [];

export function getServerToasts(): Toast[] {
  return NO_TOASTS;
}
