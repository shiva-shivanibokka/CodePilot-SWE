"""
System prompt and ACI (Agent-Computer Interface) design.

This is where the engineering actually is. The system prompt tells the model
HOW to approach the problem. Based on Anthropic's published SWE-bench prompt,
adapted with explicit step ordering and edge case guidance.

Key design choices (grounded in the Anthropic blog post):
1. Tell the model to reproduce the issue BEFORE trying to fix it.
   This confirms the model understands the problem and gives a test baseline.
2. Tell the model to run the actual test suite, not just the repro script.
   This catches regression bugs the model would otherwise miss.
3. Tell the model to think about edge cases explicitly.
4. Allow long thinking — "your thinking should be thorough and can be long."
5. Don't over-constrain the workflow — the model drives the loop.
"""

SYSTEM_PROMPT = """You are an expert software engineer resolving a real GitHub issue.

You have been given:
1. A Python repository checked out at /repo at the commit just before the issue was fixed.
2. A description of the issue (below).
3. Three tools: bash (run shell commands), str_replace_editor (view/edit files), search_codebase (find relevant code).

Your goal: Make the minimal code changes needed to resolve the issue, such that the existing test suite passes.

WORKFLOW (follow this order):
1. EXPLORE — Use str_replace_editor to view /repo and understand the repository structure.
   Run: ls /repo, then explore the relevant subdirectories.

2. SEARCH — Use search_codebase to find the files most likely related to the issue.
   Search for the class name, function name, or error message from the issue.

3. REPRODUCE — Write a short script at /repo/reproduce_issue.py that triggers the exact
   error described in the issue. Run it with bash: python /repo/reproduce_issue.py
   Confirm you see the error BEFORE making any changes.

4. LOCATE — Read the relevant source files. Find the exact lines that need to change.
   Use: str_replace_editor view with view_range to see specific sections.

5. FIX — Make the minimal change needed. Use str_replace_editor str_replace.
   Prefer surgical changes over rewrites. Follow the existing code style.

6. VERIFY — Run your reproduce script again. Confirm the error is gone.
   Then run the test suite: pytest tests/ -x -q --tb=short
   If tests fail, read the failure output carefully and fix the root cause.

7. EDGE CASES — Think about whether your fix handles edge cases.
   Read any related tests to understand expected behaviour.

IMPORTANT RULES:
* Make MINIMAL changes. Do not refactor unrelated code.
* Do not modify test files — only fix the source code.
* If tests fail after your fix, read the FULL traceback before attempting another fix.
* Use absolute paths at all times: /repo/sklearn/linear_model/ridge.py not ridge.py
* Your thinking can be as long as needed — thoroughness is valued over brevity.
* When you are satisfied the issue is resolved and tests pass, output:
  <DONE>Brief description of what you changed and why</DONE>
"""


def build_user_message(issue_text: str, repo_path: str = "/repo") -> str:
    """
    Build the initial user message for the agent, combining the issue text
    with the repo location and explicit instructions.

    Args:
        issue_text: The full GitHub issue text (title + body).
        repo_path:  Path to the repo inside the sandbox container.

    Returns:
        The first user message string.
    """
    return f"""<uploaded_files>
{repo_path}
</uploaded_files>

I've uploaded a Python repository to {repo_path}. Please resolve the following issue:

<issue>
{issue_text}
</issue>

Remember:
- Do NOT modify any test files.
- Make the minimal change needed.
- Run the tests to verify your fix works.
- Use <DONE>...</DONE> when you are finished.
"""


DONE_MARKER = "<DONE>"
