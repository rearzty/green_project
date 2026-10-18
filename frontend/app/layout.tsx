import type { Metadata } from "next";
// Шрифты — из npm-пакетов, а не с fonts.googleapis.com: интерфейс не должен
// зависеть от внешней сети (на медленном канале страница ждала бы шрифт, а
// без сети уходила бы в системный). Кириллица и латиница отдельными файлами
// — браузер подтянет только нужные по unicode-range.
import "@fontsource/onest/latin-400.css";
import "@fontsource/onest/latin-500.css";
import "@fontsource/onest/latin-600.css";
import "@fontsource/onest/cyrillic-400.css";
import "@fontsource/onest/cyrillic-500.css";
import "@fontsource/onest/cyrillic-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "@fontsource/jetbrains-mono/cyrillic-400.css";
import "@fontsource/jetbrains-mono/cyrillic-500.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "GreenProject — озеленение города",
  description: "Автоматическое проектирование озеленения с учётом подземных коммуникаций",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body className="h-screen overflow-hidden bg-gp-panel font-sans text-gp-text antialiased">{children}</body>
    </html>
  );
}
