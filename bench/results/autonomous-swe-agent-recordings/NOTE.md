# Recordings carried over from Autonomous-SWE-Agent

Eight recorded runs (four SWE-bench Lite instances x two arms), made with
Autonomous-SWE-Agent's own agent loop and agentless pipeline on
`claude-sonnet-5`, local backend, in August 2026. They were the replay data
for that project's frontend (`frontend/public/demo/`), moved here unchanged
when the frontend was dropped. `index.json` lists them.

**They are not results of this repository's agent or harness**, and they were
graded by the harness this merge replaced: a 20-test cap, `-k` substring
selection for bare test names, `-x`, and a clone that kept the repository's
later history. Read the `resolved` flags with that in mind. The four instances
were chosen by hand, not sampled.

Each file's `setup` field is the environment command that made the instance
build on Windows; `bench/setups.json` reuses those commands.
