import { NextResponse } from "next/server";

// Hosted-demo mirror of agent/providers.py (the source of truth — keep in sync).
// Served from the Vercel app itself so the hosted site needs no separate backend.
// live_runs_enabled is false here: real agent runs need a Docker sandbox and run
// locally (point NEXT_PUBLIC_API_BASE at the local Python API to enable them).
const PROVIDERS = [
  {
    key: "anthropic",
    label: "Anthropic",
    keyUrl: "https://console.anthropic.com/settings/keys",
    models: [
      { id: "claude-opus-4-8", label: "Claude Opus 4.8" },
      { id: "claude-sonnet-5", label: "Claude Sonnet 5" },
      { id: "claude-haiku-4-5", label: "Claude Haiku 4.5" },
    ],
  },
  {
    key: "openai",
    label: "OpenAI",
    keyUrl: "https://platform.openai.com/api-keys",
    models: [
      { id: "gpt-5.6-sol", label: "GPT-5.6 Sol (flagship)" },
      { id: "gpt-5.6-terra", label: "GPT-5.6 Terra (balanced)" },
      { id: "gpt-5.6-luna", label: "GPT-5.6 Luna (fast/cheap)" },
    ],
  },
  {
    key: "google",
    label: "Google Gemini",
    keyUrl: "https://aistudio.google.com/apikey",
    models: [
      { id: "gemini-3.1-pro", label: "Gemini 3.1 Pro" },
      { id: "gemini-3.5-flash", label: "Gemini 3.5 Flash" },
      { id: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
    ],
  },
  {
    key: "groq",
    label: "Groq",
    keyUrl: "https://console.groq.com/keys",
    models: [
      { id: "llama-3.3-70b-versatile", label: "Llama 3.3 70B" },
      { id: "openai/gpt-oss-120b", label: "GPT-OSS 120B" },
      { id: "llama-3.1-8b-instant", label: "Llama 3.1 8B (instant)" },
    ],
  },
];

export function GET() {
  return NextResponse.json({ providers: PROVIDERS, live_runs_enabled: false });
}
