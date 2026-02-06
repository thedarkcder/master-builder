import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "master-builder admin-ui",
  description: "Next.js admin dashboard for master-builder"
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
