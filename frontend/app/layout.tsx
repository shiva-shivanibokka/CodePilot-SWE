import type { Metadata } from "next";
import { Inter_Tight, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// Inter Tight for display and body — the tighter widths hold a large headline
// without the quirk of a geometric face; JetBrains Mono for every figure, path
// and log line, which is most of this page.
// Both are variable fonts: omitting `weight` loads the full axis, so the
// stylesheet can use in-between weights like 650 for display type.
const interTight = Inter_Tight({
  subsets: ["latin"],
  variable: "--font-inter-tight",
});
const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
});

export const metadata: Metadata = {
  title: "Autonomous SWE Agent — a recorded run, graded",
  description:
    "An autonomous software-engineering agent that resolves real GitHub issues two ways — an agentic tool-use loop and a 3-phase agentless pipeline. Watch a real recorded run, graded against SWE-bench's own tests, with every token and dollar accounted for.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${interTight.variable} ${jetbrains.variable}`}>
      <body>{children}</body>
    </html>
  );
}
