import type { GeoJSONFeature } from "@/lib/api";
import { CROWN_LABELS } from "@/lib/format";

export type SimilarKey = "row" | "species" | "crown" | "violations" | "manual";

export interface SimilarOption {
  key: SimilarKey;
  /** Название пункта подменю «Выделить похожие». */
  label: string;
  /** Что будет выделено — вместе с самой посадкой, от которой считали. */
  ids: string[];
  /** Название выделения для панели у карты: «Ряд», «Той же породы · Липа …». */
  name: string;
}

interface Props {
  id?: string;
  planting_type?: string;
  species?: string;
  crown?: string | null;
  group_id?: string | null;
  scheme?: string;
  is_manual_edit?: boolean;
}

const SCHEME_LABELS: Record<string, string> = { row: "Весь ряд", group: "Вся группа", curtain: "Вся куртина" };
const SCHEME_NAMES: Record<string, string> = { row: "Ряд", group: "Группа деревьев", curtain: "Куртина" };

const capitalize = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

/** Что можно выделить «похожим» на посадку `referenceId`: весь её ряд (или
 * группу/куртину), все той же породы, такой же класс кроны, все с нарушениями,
 * все изменённые вручную. Один проход по плану; пункт без смысла (ряд из одной
 * посадки, порода, которой больше нет) не предлагается. «Нарушения» и «вручную»
 * показываются, если они вообще есть, — это перечни плана, а не «похожие». */
export function similarOptions(features: GeoJSONFeature[], referenceId: string, violationIds: ReadonlySet<string>): SimilarOption[] {
  const reference = features.find((f) => (f.properties as Props).id === referenceId)?.properties as Props | undefined;
  if (!reference) return [];

  const row: string[] = [];
  const species: string[] = [];
  const crown: string[] = [];
  const violations: string[] = [];
  const manual: string[] = [];
  const sameSpecies = reference.species && reference.species !== "default" ? reference.species : null;

  for (const feature of features) {
    const p = feature.properties as Props;
    if (!p.id) continue;
    if (reference.group_id && p.group_id === reference.group_id) row.push(p.id);
    if (sameSpecies && p.species === sameSpecies && p.planting_type === reference.planting_type) species.push(p.id);
    if (reference.crown && p.planting_type === "tree" && p.crown === reference.crown) crown.push(p.id);
    if (violationIds.has(p.id)) violations.push(p.id);
    if (p.is_manual_edit) manual.push(p.id);
  }

  const options: SimilarOption[] = [];
  const scheme = reference.scheme ?? "";
  if (row.length > 1 && SCHEME_LABELS[scheme]) options.push({ key: "row", label: SCHEME_LABELS[scheme], ids: row, name: SCHEME_NAMES[scheme] });
  if (species.length > 1) options.push({ key: "species", label: "Той же породы", ids: species, name: sameSpecies ?? "Той же породы" });
  if (crown.length > 1 && reference.crown && CROWN_LABELS[reference.crown]) {
    const name = capitalize(CROWN_LABELS[reference.crown]);
    options.push({ key: "crown", label: name, ids: crown, name });
  }
  if (violations.length > 0) options.push({ key: "violations", label: "Все с нарушениями", ids: violations, name: "С нарушениями" });
  if (manual.length > 0) options.push({ key: "manual", label: "Изменённые вручную", ids: manual, name: "Изменённые вручную" });
  return options;
}
