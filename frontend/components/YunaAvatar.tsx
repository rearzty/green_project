"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

/** Лицо Юны — одно на все места (шапка, чат, инспектор, палитра команд).
 * Нет картинки — буква «Ю» в кружке, остальное работает так же. */
export function YunaAvatar({ className, letterClassName }: { className: string; letterClassName?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span
        className={cn(
          className,
          "flex flex-none items-center justify-center rounded-full bg-[#3A4A2C] font-semibold text-gp-accent-text",
          letterClassName
        )}
      >
        Ю
      </span>
    );
  }
  return (
    <img
      src="/yuna-avatar.png"
      alt="Юна"
      className={cn(className, "flex-none rounded-full object-cover")}
      onError={() => setFailed(true)}
    />
  );
}
