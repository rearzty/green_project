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

/** «у <чего>» для названия ограничения — в родительном падеже: заголовок
 * строки нарушения читается «Дерево у теплосети». Ключи — object_type из
 * geo_engine/compliance.py::OBJECT_LABELS_RU (там подписи в именительном). */
export const OBJECT_GENITIVE_RU: Record<string, string> = {
  heat_network: "теплосети",
  water_pipe: "водопровода",
  sewer: "канализации",
  gas_pipe: "газопровода",
  cable_line: "кабеля",
  power_line_corridor: "ЛЭП",
  building: "стены здания",
  road: "проезжей части",
  sidewalk: "тротуара",
  sidewalk_kerb: "бордюра",
  lighting_pole: "опоры освещения",
  retaining_wall: "подпорной стенки",
  tram_track: "трамвайных путей",
  neighbour_tree: "соседнего дерева",
  school_kindergarten: "школы или детского сада",
  slope_toe: "подошвы откоса",
};

/** Расстояние по-русски: «4,6 м». Один знак после запятой, а два — когда
 * округление до одного скрыло бы нарушение (1,96 м при норме 2 м не должно
 * превращаться в «2 м · норма 2 м»). */
export function formatMeters(m: number, digits = 1): string {
  return `${m.toLocaleString("ru-RU", { minimumFractionDigits: 0, maximumFractionDigits: digits })} м`;
}

export function formatDistancePair(actual: number, required: number): { actual: string; required: string } {
  const a1 = formatMeters(actual, 1);
  const r1 = formatMeters(required, 1);
  if (a1 === r1 && Math.abs(actual - required) > 1e-9) return { actual: formatMeters(actual, 2), required: formatMeters(required, 2) };
  return { actual: a1, required: r1 };
}

/** Класс кроны по-русски (geo_engine/species.py: wide / medium / narrow). */
export const CROWN_LABELS: Record<string, string> = { wide: "широкая крона", medium: "средняя крона", narrow: "узкая крона" };
