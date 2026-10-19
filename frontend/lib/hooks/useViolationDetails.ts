"use client";

import { useEffect, useRef, useState } from "react";

import { getItemsCompliance, type ItemCompliance } from "@/lib/api";

/** Сколько нарушений дозапрашиваем за один раз: обоснование считает сервер
 * (STRtree по всем сетям плана), и просить его сразу про сотни строк, из
 * которых экран показывает двадцать, незачем. */
const BATCH = 60;

/** Подробности нарушений для списка «Нужно исправить»: чем именно мешает
 * посадка и сколько метров не хватает. Список нарушений (ValidationViolation)
 * несёт только общую фразу, а «1,6 м · норма 2 м» берётся из обоснования
 * посадки — того же, что открывает инспектор.
 *
 * Кэш — по `planId:itemId:planRevision`: правка, тронувшая посадку, меняет
 * расстояния, и запись устаревает вместе с ревизией плана. Запрос на смену
 * плана/проекта отбрасывается по seq (устаревший ответ не должен лечь в кэш
 * нового плана). */
export function useViolationDetails(
  projectId: string | undefined,
  planId: string | undefined,
  planRevision: number,
  itemIds: readonly string[]
): ReadonlyMap<string, ItemCompliance> {
  const cacheRef = useRef(new Map<string, ItemCompliance>());
  const inflightRef = useRef(new Set<string>());
  const [, setVersion] = useState(0);

  const key = (id: string) => `${planId}:${planRevision}:${id}`;
  // Сам ключ списка — чтобы эффект не срабатывал на каждый новый массив.
  const wanted = itemIds.join("|");

  useEffect(() => {
    if (!projectId || !planId || itemIds.length === 0) return;
    const missing = itemIds.filter((id) => !cacheRef.current.has(key(id)) && !inflightRef.current.has(key(id))).slice(0, BATCH);
    if (missing.length === 0) return;
    missing.forEach((id) => inflightRef.current.add(key(id)));
    let cancelled = false;
    getItemsCompliance(projectId, planId, missing)
      .then((items) => {
        if (cancelled) return;
        items.forEach((item) => cacheRef.current.set(key(item.item_id), item));
        setVersion((v) => v + 1);
      })
      .catch(() => {
        // Без подробностей строка остаётся с общей фразой сервера — список
        // от этого не пропадает.
      })
      .finally(() => missing.forEach((id) => inflightRef.current.delete(key(id))));
    return () => {
      cancelled = true;
      missing.forEach((id) => inflightRef.current.delete(key(id)));
    };
    // key() читает planId/planRevision, которые уже в зависимостях
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, planId, planRevision, wanted]);

  const result = new Map<string, ItemCompliance>();
  itemIds.forEach((id) => {
    const hit = cacheRef.current.get(key(id));
    if (hit) result.set(id, hit);
  });
  return result;
}
