import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-40",
  {
    variants: {
      variant: {
        // Главное действие панели/экрана — акцент; на экране он один.
        default: "bg-gp-accent font-semibold text-gp-on-accent hover:bg-gp-accent-hover",
        // «Зелёная» вторичная: выбранные чипы, мягкие подтверждения.
        soft: "border border-gp-accent-soft-border bg-gp-accent-soft text-gp-text hover:bg-[#344230]",
        outline: "border border-gp-border bg-transparent text-gp-text hover:bg-gp-hover",
        // Текстовая кнопка-ссылка (акцентный цвет без фона).
        ghost: "text-gp-accent hover:bg-gp-hover",
        secondary: "border border-gp-border bg-transparent text-gp-text hover:bg-gp-hover",
      },
      size: {
        default: "h-10 px-4",
        lg: "h-[42px] px-4 text-sm",
        sm: "h-9 px-3 text-[13px]",
        icon: "h-9 w-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  )
);
Button.displayName = "Button";
