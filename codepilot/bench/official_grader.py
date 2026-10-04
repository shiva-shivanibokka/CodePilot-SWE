"""
An optional second grader: the official SWE-bench evaluation, per instance.

Copied from the Autonomous-SWE-Agent SOP-eval worktree (`eval_sop/grader.py`,
uncommitted there, read on 2026-10-04) with two changes (docs/MERGE_DECISIONS.md, D37):

* **No "already-applied" fallback.** The original treated a patch that failed
  to apply but *reverse*-applied cleanly as applied. That only happens when the
  change is already in the image, and then the tests run against code the
  agent did not write. Here a patch that does not apply is not applied.
* **No image pulls unless asked.** Official instance images are several GB
  each (the flask-4992 image on this machine is 4.23 GB); `ensure_image`
  refuses to pull unless `allow_pull=True`.

What is official (imported from the `swebench` package, which is optional and
not in requirements.txt): the per-instance eval script
(`make_test_spec(...).eval_script`, which resets the test files, applies the
test patch and runs the repository's own test command), the patch-application
command chain (`GIT_APPLY_CMDS`), the per-repository log parser, and
`get_eval_report`, which decides "resolved" by exact test id with no cap.

What is ours: the container is created with `network_mode="none"`, because
the patches come from an unvetted model. An instance whose official eval
needs the network fails its gold check and is excluded.

Use it to cross-check `swebench.grade` on the final submitted patches:
`pip install swebench` first.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from pathlib import Path, PurePosixPath

GRADE_TIMEOUT = 1800  # the official harness default
WORKDIR = "/testbed"


def _swebench():
    """The official functions, imported only when grading."""
    from swebench.harness.constants import CONTAINER_PATCH_FILE
    from swebench.harness.docker_utils import copy_to_container, exec_run_with_timeout
    from swebench.harness.grading import get_eval_report
    from swebench.harness.run_evaluation import GIT_APPLY_CMDS
    from swebench.harness.utils import make_test_spec

    from codepilot.bench import swebench_compat  # noqa: F401 - stubs datasets/modal

    return {
        "CONTAINER_PATCH_FILE": CONTAINER_PATCH_FILE,
        "copy_to_container": copy_to_container,
        "exec_run_with_timeout": exec_run_with_timeout,
        "get_eval_report": get_eval_report,
        "GIT_APPLY_CMDS": GIT_APPLY_CMDS,
        "make_test_spec": make_test_spec,
    }


def ensure_image(client, image: str, *, allow_pull: bool = False, retries: int = 30) -> None:
    """Use the instance image if present; pull it only when `allow_pull`."""
    import docker

    try:
        client.images.get(image)
        return
    except docker.errors.ImageNotFound:
        if not allow_pull:
            raise RuntimeError(
                f"{image} is not present and pulling was not allowed "
                "(official images are several GB each; pass allow_pull=True)"
            ) from None
    last = None
    for _ in range(retries):
        try:
            client.images.pull(image)
            return
        except Exception as exc:  # noqa: BLE001 - Docker Hub drops large layers
            last = exc
            time.sleep(3)
    raise RuntimeError(f"could not pull {image}: {last}")


def apply_in_container(container, patch_path: str, apply_cmds: list[str]) -> tuple[bool, str | None, str]:
    """Try each official apply command in order. Returns (applied, cmd, output).

    A patch that only reverse-applies is *not* applied.
    """
    output = ""
    for attempt, cmd in enumerate(apply_cmds):
        if attempt:
            container.exec_run(["/bin/bash", "-c", "git checkout -- . ; git clean -fd"], workdir=WORKDIR)
        val = container.exec_run(f"{cmd} {patch_path}", workdir=WORKDIR)
        output = (val.output or b"").decode("utf-8", "replace")
        if val.exit_code == 0:
            return True, cmd, output
    return False, None, output


def grade_patch(
    instance: dict,
    patch: str | None,
    log_dir: Path,
    *,
    skip_patch: bool = False,
    timeout: int = GRADE_TIMEOUT,
    allow_pull: bool = False,
) -> dict:
    """Apply `patch` in a fresh, network-less container of the instance image
    and run the official eval script. Raw logs go to `log_dir`."""
    import docker

    sb = _swebench()
    log_dir.mkdir(parents=True, exist_ok=True)
    spec = sb["make_test_spec"](instance)
    client = docker.from_env(timeout=1800)
    ensure_image(client, spec.image, allow_pull=allow_pull)
    out: dict = {
        "instance_id": instance["instance_id"], "image": spec.image, "resolved": False,
        "applied": False, "apply_cmd": None, "timed_out": False, "error": None,
    }
    if not skip_patch and not (patch or "").strip():
        out["error"] = "empty patch"
        (log_dir / "grade.json").write_text(json.dumps(out, indent=2))
        return out

    container = None
    t0 = time.monotonic()
    try:
        container = client.containers.run(
            image=spec.image, name=f"codepilot-grade-{uuid.uuid4().hex[:10]}",
            command="tail -f /dev/null", detach=True, user="root",
            network_mode="none", mem_limit="6g", labels={"codepilot": "official-grade"},
        )
        if not skip_patch:
            patch_file = log_dir / "patch.diff"
            patch_file.write_text(patch, encoding="utf-8", newline="\n")
            sb["copy_to_container"](container, patch_file, PurePosixPath(sb["CONTAINER_PATCH_FILE"]))
            applied, cmd, output = apply_in_container(
                container, sb["CONTAINER_PATCH_FILE"], list(sb["GIT_APPLY_CMDS"]))
            out["applied"], out["apply_cmd"] = applied, cmd
            if not applied:
                out["error"] = "patch did not apply"
                (log_dir / "apply_fail.txt").write_text(output, encoding="utf-8")
                return out
        else:
            out["applied"], out["apply_cmd"] = True, "skipped (no patch)"

        eval_file = log_dir / "eval.sh"
        eval_file.write_text(spec.eval_script, encoding="utf-8", newline="\n")
        sb["copy_to_container"](container, eval_file, PurePosixPath("/eval.sh"))
        test_output, timed_out, runtime = sb["exec_run_with_timeout"](container, "/bin/bash /eval.sh", timeout)
        out["timed_out"], out["test_runtime_s"] = timed_out, round(runtime, 1)
        test_log = log_dir / "test_output.txt"
        test_log.write_text(test_output, encoding="utf-8")
        if timed_out:
            out["error"] = f"tests timed out after {timeout}s"
            return out
        pred = {"instance_id": instance["instance_id"], "model_name_or_path": "codepilot",
                "model_patch": patch if not skip_patch else ""}
        report = sb["get_eval_report"](spec, pred, str(test_log), include_tests_status=True)
        r = report[instance["instance_id"]]
        out["resolved"], out["report"] = bool(r.get("resolved")), r
        return out
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        out["wall_s"] = round(time.monotonic() - t0, 1)
        if container is not None:
            with contextlib.suppress(Exception):
                container.remove(force=True)
        (log_dir / "grade.json").write_text(json.dumps(out, indent=2, default=str))
