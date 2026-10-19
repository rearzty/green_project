import type { LucideIcon } from "lucide-react";

/** Действие палитры команд (Ctrl+K). Реестр собирает страница из тех же
 * функций, что вызывают кнопки панелей и команды Юны, — отдельной логики у
 * палитры нет. В список попадает только то, что доступно прямо сейчас:
 * серая строка, которую нельзя выполнить, в быстром поиске мешает. */
export interface Command {
  id: string;
  title: string;
  /** Слова для поиска помимо названия: «ошибки» находят «нарушения». */
  keywords?: string[];
  /** Справа: горячая клавиша или число объектов. */
  meta?: string;
  icon?: LucideIcon;
  /** Оранжевая точка вместо значка — действие про нарушения. */
  dot?: boolean;
  run: () => void;
}

export interface CommandMatch {
  command: Command;
  /** [начало, конец) совпадений в названии — для выделения жирным. */
  ranges: [number, number][];
}

// ё=е и регистр не важны. Замена посимвольная, поэтому индексы в нормализованной
// строке совпадают с индексами в исходной и по ним можно резать название.
export const normalizeQuery = (text: string) => text.toLowerCase().replace(/ё/g, "е");

const isWordStart = (text: string, index: number) => index === 0 || /[^a-zа-я0-9]/.test(text[index - 1]);

/** Нечёткий поиск: каждое слово запроса должно найтись в названии или в
 * синонимах (с начала слова — лучше, в середине — хуже); порядок слов не
 * важен. Пустой запрос — всё, в порядке реестра. */
export function searchCommands(commands: Command[], query: string): CommandMatch[] {
  const tokens = normalizeQuery(query).split(/\s+/).filter(Boolean);
  if (tokens.length === 0) return commands.map((command) => ({ command, ranges: [] }));

  const scored: { match: CommandMatch; score: number; order: number }[] = [];
  commands.forEach((command, order) => {
    const title = normalizeQuery(command.title);
    const keywords = (command.keywords ?? []).map(normalizeQuery);
    let score = 0;
    const ranges: [number, number][] = [];
    for (const token of tokens) {
      const inTitle = title.indexOf(token);
      if (inTitle >= 0) {
        score += isWordStart(title, inTitle) ? 3 : 2;
        ranges.push([inTitle, inTitle + token.length]);
        continue;
      }
      const inKeywords = keywords.findIndex((k) => k.split(/[^a-zа-я0-9]+/).some((word) => word.startsWith(token)));
      if (inKeywords >= 0) {
        score += 1.5;
        continue;
      }
      return; // слово не нашлось нигде — команда не подходит
    }
    scored.push({ match: { command, ranges: mergeRanges(ranges) }, score, order });
  });
  return scored.sort((a, b) => b.score - a.score || a.order - b.order).map((entry) => entry.match);
}

function mergeRanges(ranges: [number, number][]): [number, number][] {
  const sorted = [...ranges].sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const range of sorted) {
    const last = merged[merged.length - 1];
    if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
    else merged.push([range[0], range[1]]);
  }
  return merged;
}
