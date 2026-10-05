import type { Metadata, Viewport } from "next";
import { Manrope } from "next/font/google";
import "./globals.css";

const manrope = Manrope({ subsets: ["latin"], variable: "--font-manrope", display: "swap", weight: ["200", "300", "400", "500", "600", "700", "800"] });
export const metadata: Metadata = { title: "Master Builder", description: "An open-source software factory for cloud-based engineering automation." };
export const viewport: Viewport = { width: "device-width", initialScale: 1 };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="en"><body className={`min-h-screen font-sans antialiased ${manrope.variable}`}>{children}</body></html>;
}
