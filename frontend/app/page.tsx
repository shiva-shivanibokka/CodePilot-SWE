import { Hero } from "@/components/Hero";
import { Benchmark } from "@/components/Benchmark";
import { DemoConsole } from "@/components/DemoConsole";
import { Architecture } from "@/components/Architecture";
import { RunItYourself } from "@/components/RunItYourself";
import Tabs, { type TabDef } from "@/components/Tabs";

const REPO = "https://github.com/shiva-shivanibokka/Autonomous-SWE-Agent";

// The recorded run leads, because it is the only thing here that is measured.
const TABS: TabDef[] = [
  { id: "run", label: "Watch it work", note: "recorded", content: <DemoConsole /> },
  { id: "benchmark", label: "Benchmark", content: <Benchmark /> },
  { id: "architecture", label: "Architecture", content: <Architecture /> },
  { id: "local", label: "Run it yourself", content: <RunItYourself /> },
];

export default function Page() {
  return (
    <>
      <nav className="masthead">
        <div className="wrap masthead-inner">
          <span className="brand">
            <span className="brand-mark">+</span>
            swe-agent
          </span>
          <div className="masthead-right">
            <span className="verdict-chip">resolved</span>
            <span className="hide-sm">pallets__flask-4992</span>
            <a href={REPO} target="_blank" rel="noreferrer">
              Source ↗
            </a>
          </div>
        </div>
      </nav>

      <main>
        <Hero />
        <Tabs tabs={TABS} />
      </main>

      <footer className="footer">
        <div className="wrap footer-inner">
          <span className="byline">
            Built by <strong>Shivani Bokka</strong>
          </span>
          <span className="footer-meta">
            <a href={REPO} target="_blank" rel="noreferrer">
              Source on GitHub
            </a>
            <span>· Bring your own key · No key stored ·</span>
            <span>The run above is a recording, not a live service</span>
          </span>
        </div>
      </footer>
    </>
  );
}
