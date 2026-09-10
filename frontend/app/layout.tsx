import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GreenProject — озеленение города",
  description: "Автоматическое проектирование озеленения с учётом подземных коммуникаций",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body className="h-screen overflow-hidden bg-stone-50 text-stone-900">{children}</body>
    </html>
  );
}
