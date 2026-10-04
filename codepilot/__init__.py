"""CodePilot: a small coding agent that edits real repositories."""

import os

# Prices come from the cost map bundled with the pinned LiteLLM release, never
# from the copy LiteLLM otherwise downloads at import time: a run's costs must
# not depend on the day it ran (docs/MERGE_DECISIONS.md, D25). Import codepilot
# before litellm for this to take effect.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

__version__ = "0.2.0"
