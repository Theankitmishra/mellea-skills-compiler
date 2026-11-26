"""Pi target: translate a LoadedContext into a TranslationPlan.

Supported modalities:
  synchronous_oneshot   — sync Python subprocess via scripts/run.sh
  streaming             — unbuffered async streaming via scripts/run.sh
  conversational_session — session carry-forward via --session JSON arg
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

from mellea_skills_compiler.export.targets._shell import (
    _guardian_inline_snippet,  # noqa: F401 - re-exported for tests
    _render_run_sh,
    _skill_md_arg_note,
)


if TYPE_CHECKING:
    from mellea_skills_compiler.export.exporter import (
        AdapterFile,
        LoadedContext,
        ParsedSignature,
        TranslationPlan,
    )

SUPPORTED_MODALITIES = {
    "synchronous_oneshot",
    "streaming",
    "conversational_session",
}


def _to_pi_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    s = s[:64].strip("-")
    return s or "pipeline"


def translate_pi(loaded: "LoadedContext") -> "TranslationPlan":
    from mellea_skills_compiler.export.exporter import (
        AdapterFile,
        TranslationPlan,
        get_skill_description,
    )

    manifest = loaded.manifest
    sig = loaded.sig
    warnings: list[str] = []

    package_name = manifest["package_name"]
    skill_name = _to_pi_name(package_name)
    modality = manifest.get("modality", "synchronous_oneshot")
    export_version = _export_version()
    description = get_skill_description(loaded)

    if modality not in SUPPORTED_MODALITIES:
        warnings.append(
            f"Modality '{modality}' is not fully supported by the pi target. "
            "Falling back to synchronous_oneshot adapter."
        )
        modality = "synchronous_oneshot"

    run_sh = _render_run_sh(
        modality=modality,
        package_name=package_name,
        entry_module=loaded.entry_module,
        entry_function=sig.function_name,
        pattern=sig.pattern,
        params=sig.params,
        export_version=export_version,
        has_policy_manifest=loaded.policy_manifest_path is not None,
        enforce=loaded.invocation.enforce,
    )

    skill_md = _render_skill_md(
        manifest=manifest,
        skill_name=skill_name,
        modality=modality,
        sig=sig,
        description=description,
    )

    pyproject_toml = _render_pyproject_toml(
        skill_name=skill_name,
        package_name=package_name,
        has_policy_manifest=loaded.policy_manifest_path is not None,
    )

    readme = _render_readme(
        skill_name=skill_name,
        package_name=package_name,
        modality=modality,
        sig=sig,
        has_policy_manifest=loaded.policy_manifest_path is not None,
    )

    adapter_files: list["AdapterFile"] = [
        AdapterFile("SKILL.md", skill_md),
        AdapterFile("scripts/run.sh", run_sh),
        AdapterFile("pyproject.toml", pyproject_toml),
        AdapterFile("README.md", readme),
    ]

    deployment_guidance = _deployment_guidance(modality, skill_name)

    return TranslationPlan(
        graph_name=skill_name,
        adapter_files=adapter_files,
        bundled_package_name=package_name,
        warnings=warnings,
        deployment_guidance=deployment_guidance,
    )


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------


def _export_version() -> str:
    from mellea_skills_compiler.export.exporter import EXPORT_VERSION

    return EXPORT_VERSION


# ---------------------------------------------------------------------------
# SKILL.md
# ---------------------------------------------------------------------------

_MAX_DESCRIPTION_LEN = 1024


def _render_skill_md(
    *,
    manifest: dict,
    skill_name: str,
    modality: str,
    sig: "ParsedSignature",
    description: Optional[str] = None,
) -> str:
    if description is None:
        from mellea_skills_compiler.export.exporter import get_skill_description

        description = get_skill_description(manifest=manifest)
    if len(description) > _MAX_DESCRIPTION_LEN:
        description = description[:_MAX_DESCRIPTION_LEN]
    display_name = skill_name.replace("-", " ").title()

    arg_note = _skill_md_arg_note(sig, modality)

    return (
        f"---\n"
        f"name: {skill_name}\n"
        f"description: {json.dumps(description)}\n"
        f'compatibility: "Requires Python >=3.11 and the bundled package installed."\n'
        f"---\n"
        f"\n"
        f"# {display_name}\n"
        f"\n"
        f"{description}\n"
        f"\n"
        f"## Invocation\n"
        f"\n"
        f"This skill is invoked by running `scripts/run.sh`.{arg_note}\n"
        f"\n"
        f"## Output format\n"
        f"\n"
        f'stdout: `{{"status": "ok", "output": <result>}}`\n'
        f'stderr on error: `{{"status": "error", "message": <description>}}`\n'
        f"\n"
        f"Exit code 0 on success, non-zero on error.\n"
    )


# ---------------------------------------------------------------------------
# pyproject.toml
# ---------------------------------------------------------------------------


def _render_pyproject_toml(
    *, skill_name: str, package_name: str, has_policy_manifest: bool = False
) -> str:
    from mellea_skills_compiler.constants import MELLEA_PIN
    deps = [f'    "{MELLEA_PIN}",\n']
    if has_policy_manifest:
        deps.append(
            '    "mellea-skills-compiler@git+https://github.com/generative-computing/mellea-skills-compiler.git",\n'
        )

    return (
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        f'name = "{skill_name}-pi-adapter"\n'
        'version = "0.1.0"\n'
        f'description = "Pi adapter for {skill_name} Mellea pipeline"\n'
        'requires-python = ">=3.11"\n'
        "dependencies = [\n" + "".join(deps) + "]\n"
        "\n"
        "[tool.setuptools.packages.find]\n"
        'where = ["."]\n'
        f'include = ["{package_name}*"]\n'
    )


# ---------------------------------------------------------------------------
# README.md
# ---------------------------------------------------------------------------

_MODALITY_NOTES = {
    "synchronous_oneshot": (
        "This skill uses **synchronous oneshot** invocation. "
        "Each call runs the pipeline to completion and returns a single JSON result."
    ),
    "streaming": (
        "This skill uses **streaming** invocation. "
        "Output is printed incrementally as it is generated."
    ),
    "conversational_session": (
        "This skill uses **conversational session** invocation. "
        "Pass `--session` as a JSON array of prior turns to maintain conversation history."
    ),
}

_MODALITY_INVOCATION_EXAMPLE = {
    "synchronous_oneshot": "bash scripts/run.sh <arg1> [arg2 ...]",
    "streaming": "bash scripts/run.sh <arg1> [arg2 ...]",
    "conversational_session": (
        'bash scripts/run.sh --input "your message here"\n'
        "# With session history:\n"
        'bash scripts/run.sh --input "follow-up" '
        '--session \'[{"input": "previous", "output": "response"}]\''
    ),
}


def _render_readme(
    *,
    skill_name: str,
    package_name: str,
    modality: str,
    sig: "ParsedSignature",
    has_policy_manifest: bool = False,
) -> str:
    display_name = skill_name.replace("-", " ").title()
    modality_note = _MODALITY_NOTES.get(
        modality, _MODALITY_NOTES["synchronous_oneshot"]
    )
    invocation_example = _MODALITY_INVOCATION_EXAMPLE.get(
        modality, _MODALITY_INVOCATION_EXAMPLE["synchronous_oneshot"]
    )

    install_cmd = "pip install -e ."

    guardian_section = ""
    if has_policy_manifest:
        guardian_section = (
            "\n## Guardian audit\n\n"
            "This bundle was exported from a certified skill. Guardian audit-mode is active at runtime.\n\n"
            "The audit log is written to `audit/runtime_audit.jsonl` in this directory. "
            "Ensure the process has write access:\n\n"
            "```bash\n"
            "mkdir -p audit\n"
            "```\n\n"
            "To suppress Guardian at runtime, remove `policy_manifest.json` from this directory.\n"
        )

    return (
        f"# {display_name} — Pi Adapter\n"
        f"\n"
        f"Exported Pi adapter for the `{skill_name}` Mellea pipeline.\n"
        f"\n"
        f"## Installation\n"
        f"\n"
        f"```bash\n"
        f"{install_cmd}\n"
        f"```\n"
        f"{guardian_section}"
        f"\n"
        f"## Registration\n"
        f"\n"
        f"Copy this directory to your Pi skills folder:\n"
        f"\n"
        f"```bash\n"
        f"# Project-level:\n"
        f"cp -r . .pi/skills/{skill_name}/\n"
        f"\n"
        f"# Or shared across agents:\n"
        f"cp -r . .agents/skills/{skill_name}/\n"
        f"\n"
        f"# Or user-level (available in all projects):\n"
        f"cp -r . ~/.pi/agent/skills/{skill_name}/\n"
        f"```\n"
        f"\n"
        f"## Invocation\n"
        f"\n"
        f"```bash\n"
        f"{invocation_example}\n"
        f"```\n"
        f"\n"
        f"## Modality\n"
        f"\n"
        f"{modality_note}\n"
        f"\n"
        f"## Output format\n"
        f"\n"
        f"```json\n"
        f'{{"status": "ok", "output": <result>}}\n'
        f"```\n"
        f"\n"
        f"On error (stderr):\n"
        f"\n"
        f"```json\n"
        f'{{"status": "error", "message": "<description>"}}\n'
        f"```\n"
        f"\n"
        f"Exit code 0 on success, non-zero on error.\n"
    )


# ---------------------------------------------------------------------------
# Deployment guidance
# ---------------------------------------------------------------------------


def _deployment_guidance(modality: str, skill_name: str) -> str:
    guides = {
        "synchronous_oneshot": (
            f"Install with `pip install -e .` and register under `.pi/skills/{skill_name}/` "
            f"(or `.agents/skills/{skill_name}/`). Invoke via `bash scripts/run.sh <args>`."
        ),
        "streaming": (
            f"Install with `pip install -e .` and register under `.pi/skills/{skill_name}/` "
            f"(or `.agents/skills/{skill_name}/`). Output streams incrementally — suitable for "
            "long-running or token-by-token responses."
        ),
        "conversational_session": (
            f"Install with `pip install -e .` and register under `.pi/skills/{skill_name}/` "
            f"(or `.agents/skills/{skill_name}/`). Pass `--session` JSON array across calls to "
            "maintain conversation history."
        ),
    }
    return guides.get(
        modality, f"Install with `pip install -e .` and register the skill under your pi skills root."
    )
