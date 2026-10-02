import { NextResponse } from "next/server";
import summaries from "@/data/benchmark.json";

// Real eval results, served from the Vercel app. Empty until you run the
// benchmark (python -m eval.run_eval --compare) and paste the two summary
// objects into frontend/data/benchmark.json. Never fabricated.
export function GET() {
  return NextResponse.json({ summaries });
}
