# Recorded-run replay

A static page that replays the committed Haiku study: pick one of the ten
SWE-bench Lite instances and step through what each arm did, beside the patch it
submitted and how the grader scored it.

It makes **no model call**. There is no backend, no key and no budget — it reads
JSON that `build_data.py` generated from `bench/results/haiku-study/`, which is
why it can be deployed to a free static host and left running.

## Rebuilding the data

    python replay/build_data.py

Reads `bench/results/haiku-study/main.jsonl` and writes `public/data/`. Run it
after any change to the study; nothing under `public/data/` is written by hand.

The script pushes every published string through the harness's own
`codepilot.bench.run.redact` and **fails rather than redacting**. A redaction
needed at this point would mean the committed study files are themselves
unclean — which is a bug to fix in the study, not at publication time.

## Serving it locally

    python -m http.server 4173 --directory replay/public

It must be served over HTTP rather than opened as a `file://` path, because it
fetches its data.

## What the replay can and cannot show

`harness.py::transcript_of` caps every recorded entry at 300 characters, and the
tool-result entry it writes is a one-line summary rather than the command's
output — the output was never written to disk. So the replay faithfully shows the
model's reasoning, every tool call with its arguments, and whether each call
errored, but not what the command printed back. The page says so next to each
transcript, and marks the entries that were truncated.

The patch and the grade are complete; they are not truncated.
