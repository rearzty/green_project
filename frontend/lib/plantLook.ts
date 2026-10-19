/** Как выглядит каждая порода в 3D-просмотре (components/ThreeDView.tsx):
 * форма кроны, пропорции, цвет листвы по сезонам и цвет ствола.
 *
 * Раньше все деревья в 3D были одного размера (один норматив из
 * planting_norms.yaml) и одного цвета, а форма — конус или шар — выбиралась
 * хэшем координат, то есть никак не зависела от того, липа это или берёза.
 * Теперь размер берётся из диаметра кроны породы (`crown_m` в свойствах
 * посадки, geo_engine/config/species.yaml), а остальное — из этой таблицы.
 *
 * **Высоты и цвета здесь — оформление, а не данные.** В `species.yaml` есть
 * только диаметр кроны; отношение высоты к диаметру, доля ствола и оттенки —
 * оценка по тому, как эти породы выглядят у взрослого дерева (та же честность,
 * что у высот зданий-плейсхолдеров в plan3d.ts). Размер крон при этом — реальные
 * значения плана, поэтому 3D показывает настоящую плотность посадки: широкая
 * крона дуба действительно занимает вдвое больше места, чем рябина.
 *
 * Чистый модуль без `THREE.*`, как plan3d.ts и mapStyle.ts. */

import { SEASON_PALETTES, type Season } from "@/lib/mapStyle";

/** Семейство геометрии кроны — один `InstancedMesh` на семейство. */
export type CrownForm = "lobed" | "oval" | "cone" | "bush";

export interface PlantLook {
  form: CrownForm;
  /** Высота растения к диаметру кроны (взрослое растение). */
  heightRatio: number;
  /** Доля высоты, которую занимает ствол до начала кроны; у кустов 0. */
  trunkShare: number;
  trunkColor: string;
  /** Цвет листвы/хвои в каждый сезон. */
  colors: Record<Season, string>;
}

const SNOW_CROWN = "#dfe3e5";

function season(spring: string, summer: string, autumn: string, winter: string = SNOW_CROWN): Record<Season, string> {
  return { spring, summer, autumn, winter };
}

/** Хвойные и вечнозелёные зимой остаются зелёными, а не заснеженно-белыми. */
function evergreen(spring: string, summer: string, autumn: string, winter: string): Record<Season, string> {
  return { spring, summer, autumn, winter };
}

const BARK = "#6b5844";

/** Ключ — название породы из species.yaml (как оно приходит в `species`). */
const LOOKS: Record<string, PlantLook> = {
  // Деревья. Липа и клён — плотная округлая крона на высоком стволе.
  "Липа мелколистная": { form: "lobed", heightRatio: 1.9, trunkShare: 0.32, trunkColor: BARK, colors: season("#8ccf6e", "#4f9a4a", "#dcb93d") },
  "Клён остролистный": { form: "lobed", heightRatio: 1.8, trunkShare: 0.3, trunkColor: "#6a5a4b", colors: season("#9bd16c", "#3f8f45", "#c8431e") },
  // Дуб — самая широкая крона и самый низкий ствол: растёт вширь.
  "Дуб черешчатый": { form: "lobed", heightRatio: 1.6, trunkShare: 0.28, trunkColor: "#5b4b3b", colors: season("#7fb85d", "#34733b", "#a8652b", "#d6d8d6") },
  // Берёза — узкая яйцевидная крона, белый ствол.
  "Берёза повислая": { form: "oval", heightRatio: 2.8, trunkShare: 0.4, trunkColor: "#e8e6df", colors: season("#a6dc7c", "#78bf5f", "#e3bb2f", "#e4e8ea") },
  "Рябина обыкновенная": { form: "lobed", heightRatio: 1.7, trunkShare: 0.35, trunkColor: "#6f5a48", colors: season("#9ad37c", "#5fa653", "#c0381f", "#e0dcdc") },

  // Кустарники. Цвет весной/летом — не только зелень: сирень, чубушник и спиреи
  // цветут, и на плане это читается как «разные кусты», а не один ковёр.
  "Сирень обыкновенная": { form: "bush", heightRatio: 1.1, trunkShare: 0, trunkColor: BARK, colors: season("#b99ad6", "#4f8a49", "#b6a04a") },
  "Спирея серая": { form: "bush", heightRatio: 0.8, trunkShare: 0, trunkColor: BARK, colors: season("#eef1ea", "#5d9a55", "#c9a24a") },
  "Спирея японская": { form: "bush", heightRatio: 0.8, trunkShare: 0, trunkColor: BARK, colors: season("#7fb36e", "#c97b95", "#c9763a") },
  "Спирея Бумальда": { form: "bush", heightRatio: 0.8, trunkShare: 0, trunkColor: BARK, colors: season("#7fb36e", "#c26a89", "#c9763a") },
  "Снежноягодник белый": { form: "bush", heightRatio: 0.9, trunkShare: 0, trunkColor: BARK, colors: season("#9fcf85", "#6bab62", "#b0b05a") },
  // Чубушник цветёт белым недолго (июнь), а на этом плане он — почти весь
  // кустарник: белым летом он превращал изгороди в комки ваты, поэтому цвет —
  // светлая свежая зелень без цветения.
  "Чубушник венечный": { form: "bush", heightRatio: 1.0, trunkShare: 0, trunkColor: BARK, colors: season("#a5cf8e", "#7fb56a", "#c9bb62") },
  "Лапчатка кустарниковая": { form: "bush", heightRatio: 0.7, trunkShare: 0, trunkColor: BARK, colors: season("#9cc47c", "#e3c84a", "#c7a84a") },
  "Барбарис Тунберга": { form: "bush", heightRatio: 0.8, trunkShare: 0, trunkColor: BARK, colors: season("#a05560", "#8c3a4a", "#b8402a", "#cfc2c0") },
  // Можжевельник — вечнозелёный, невысокий, коническим холмом.
  "Можжевельник казацкий": {
    form: "cone",
    heightRatio: 0.6,
    trunkShare: 0,
    trunkColor: BARK,
    colors: evergreen("#4a7c63", "#3f6f5a", "#3f6f5a", "#4d7a65"),
  },
};

/** Запас на породу, которой в таблице нет (новая в species.yaml, «default»):
 * общий вид по типу посадки и классу кроны, цвет — из сезонной палитры. */
function fallbackLook(plantingType: "tree" | "shrub", crownClass: string | null | undefined): PlantLook {
  const colors: Record<Season, string> = {
    spring: SEASON_PALETTES.spring[plantingType],
    summer: SEASON_PALETTES.summer[plantingType],
    autumn: SEASON_PALETTES.autumn[plantingType],
    winter: SEASON_PALETTES.winter[plantingType],
  };
  if (plantingType === "shrub") return { form: "bush", heightRatio: 0.9, trunkShare: 0, trunkColor: BARK, colors };
  const ratio = crownClass === "narrow" ? 2.6 : crownClass === "wide" ? 1.8 : 2.2;
  return { form: "lobed", heightRatio: ratio, trunkShare: 0.33, trunkColor: BARK, colors };
}

export function lookOf(species: string | null | undefined, plantingType: "tree" | "shrub", crownClass?: string | null): PlantLook {
  return (species ? LOOKS[species] : undefined) ?? fallbackLook(plantingType, crownClass);
}

/** Есть ли породе отдельная запись — для проверки, что каталог species.yaml и эта
 * таблица не разошлись (см. docs: новую породу в каталоге стоит добавить сюда). */
export function hasSpecificLook(species: string): boolean {
  return species in LOOKS;
}
