import type { PlantingType } from "@/lib/api";

type Forms = [one: string, few: string, many: string];

/** Russian plural form for n: 1 дерево, 2 дерева, 5 деревьев, 21 дерево. */
export function pluralRu(n: number, forms: Forms): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return forms[0];
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return forms[1];
  return forms[2];
}

export const OBJECT_FORMS: Forms = ["объект", "объекта", "объектов"];

export const PLANTING_TYPE_FORMS: Record<PlantingType, Forms> = {
  tree: ["дерево", "дерева", "деревьев"],
  shrub: ["кустарник", "кустарника", "кустарников"],
  lawn: ["газон", "газона", "газонов"],
};

export const PLANTING_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Дерево",
  shrub: "Кустарник",
  lawn: "Газон",
};

export function countLabel(n: number, forms: Forms): string {
  return `${n} ${pluralRu(n, forms)}`;
}
