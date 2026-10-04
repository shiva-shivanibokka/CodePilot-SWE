"""Import codepilot before any test module imports litellm, so the pinned
local cost map is in force for the whole test session (D25)."""

import codepilot  # noqa: F401
