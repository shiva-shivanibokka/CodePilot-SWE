import type { Metadata } from "next";
import { Bricolage_Grotesque, Inter_Tight, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// Bricolage Grotesque carries the display type — headline, section headings and
// the tabs. It has an optical-size axis and enough character to make the page
// look drawn rather than assembled, which a neutral UI grotesque cannot do on
// its own. Inter Tight sets running text underneath it, and JetBrains Mono
// takes every figure, path and log line, which is most of this page.
// All three are variable: omitting `weight` loads the full axis, so the
// stylesheet can use in-between weights like 650.
const bricolage = Bricolage_Grotesque({
  subsets: ["latin"],
  variable: "--font-bricolage",
});
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
    <html lang="en" className={`${bricolage.variable} ${interTight.variable} ${jetbrains.variable}`}>
      <body>{children}</body>
    </html>
  );
}
