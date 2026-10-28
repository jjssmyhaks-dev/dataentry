import type { Metadata } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const sans = Archivo({ variable: "--font-sans", subsets: ["latin"], display: "swap" });
const mono = JetBrains_Mono({ variable: "--font-mono", subsets: ["latin"], display: "swap" });

export const metadata: Metadata = {
  title: "Autopost — autonomous data entry",
  description:
    "Reads supplier documents, validates them against GST rules, and posts high-confidence records to TallyPrime. Uncertain records go to a small review queue.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
