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
      className={cn("w-full border-r bg-card md:w-[260px] md:min-w-[260px] md:max-w-[260px]", className)}
      {...props}
    />
  );
}

export function SidebarHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("border-b p-4", className)} {...props} />;
}

export function SidebarContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex-1 overflow-y-auto p-3", className)} {...props} />;
}

export function SidebarFooter({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("border-t p-3", className)} {...props} />;
}

export function SidebarInset({ className, ...props }: ComponentProps<"main">) {
  return <main className={cn("flex-1", className)} {...props} />;
}

export function SidebarMenu({ className, ...props }: ComponentProps<"ul">) {
  return <ul className={cn("space-y-1", className)} {...props} />;
}

export function SidebarMenuLabel({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("px-3 py-1 text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground", className)} {...props} />;
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
        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition",
        isActive ? "bg-primary text-primary-foreground" : "hover:bg-muted",
        className
      )}
      {...props}
    />
  );
}
