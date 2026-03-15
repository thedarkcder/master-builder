"use client";

import { Moon, Sun, Monitor } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  function cycle() {
    if (theme === "light") setTheme("dark");
    else if (theme === "dark") setTheme("system");
    else setTheme("light");
  }

  if (!mounted) {
    return (
      <button
        className={cn(
          "inline-flex h-8 w-8 items-center justify-center rounded-md text-sidebar-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
          className
        )}
        aria-label="Toggle theme"
      >
        <Monitor className="h-4 w-4" />
      </button>
    );
  }

  const Icon = theme === "light" ? Sun : theme === "dark" ? Moon : Monitor;
  const label = theme === "light" ? "Light mode" : theme === "dark" ? "Dark mode" : "System theme";

  return (
    <button
      onClick={cycle}
      className={cn(
        "inline-flex h-8 w-8 items-center justify-center rounded-md text-sidebar-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
        className
      )}
      aria-label={`${label} — click to cycle`}
      title={label}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
