"use client";

import type { ComponentProps } from "react";

import { Slot } from "@radix-ui/react-slot";

import { cn } from "@/lib/utils";

export function SidebarProvider({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex min-h-screen w-full bg-background", className)} {...props} />;
}

export function Sidebar({ className, ...props }: ComponentProps<"aside">) {
  return (
    <aside
      className={cn(
        "w-full bg-sidebar text-sidebar-foreground border-r border-sidebar-border md:w-[240px] md:min-w-[240px] md:max-w-[240px] flex flex-col",
        className
      )}
      {...props}
    />
  );
}

export function SidebarHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("border-b border-sidebar-border px-4 py-4", className)} {...props} />;
}

export function SidebarContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex-1 overflow-y-auto px-3 py-3", className)} {...props} />;
}

export function SidebarFooter({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("border-t border-sidebar-border px-3 py-3", className)} {...props} />;
}

export function SidebarInset({ className, ...props }: ComponentProps<"main">) {
  return <main className={cn("flex-1 min-w-0", className)} {...props} />;
}

export function SidebarMenu({ className, ...props }: ComponentProps<"ul">) {
  return <ul className={cn("space-y-0.5", className)} {...props} />;
}

export function SidebarMenuLabel({ className, ...props }: ComponentProps<"p">) {
  return (
    <p
      className={cn(
        "px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-widest text-sidebar-muted-foreground",
        className
      )}
      {...props}
    />
  );
}

export function SidebarMenuItem({ className, ...props }: ComponentProps<"li">) {
  return <li className={cn(className)} {...props} />;
}

export function SidebarMenuButton({
  className,
  asChild = false,
  isActive = false,
  ...props
}: ComponentProps<"button"> & { asChild?: boolean; isActive?: boolean }) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={cn(
        "flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
        isActive
          ? "border-l-2 border-primary bg-sidebar-accent text-sidebar-accent-foreground font-medium pl-[10px]"
          : "text-sidebar-foreground/80 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground border-l-2 border-transparent pl-[10px]",
        className
      )}
      {...props}
    />
  );
}
