"""Regression tests for shared SKILL.md description resolution and run.sh forwarding."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
import yaml

from mellea_skills_compiler.export.exporter import (
    Invocation,
    LoadedContext,
    ParsedSignature,
    get_skill_description,
    run_export,
)


_WEATHER_SKILL = Path(__file__).parents[3] / "examples/weather/weather_mellea"
_EXPECTED_WEATHER_DESC = (
    "Get current weather and forecasts via wttr.in or Open-Meteo. "
    "Use when: user asks about weather, temperature, or forecasts for any location. "
    "NOT for: historical weather data, severe weather alerts, or detailed meteorological analysis. "
    "No API key needed."
)


def _frontmatter(skill_md: str) -> dict:
    _, fm_block, _ = skill_md.split("---", 2)
    return yaml.safe_load(fm_block)


def _make_loaded(
    tmp_path: Path,
    *,
    manifest: dict | None = None,
    pkg_skill_md: str | None = None,
    root_skill_md: str | None = None,
    separate_root: bool = False,
) -> LoadedContext:
    root = tmp_path / "root"
    root.mkdir(parents=True)
    pkg_dir = (root / "demo_mellea") if separate_root else root
    pkg_dir.mkdir(exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    if pkg_skill_md is not None:
        (pkg_dir / "SKILL.md").write_text(pkg_skill_md, encoding="utf-8")
    if root_skill_md is not None and separate_root:
        (root / "SKILL.md").write_text(root_skill_md, encoding="utf-8")

    return LoadedContext(
        invocation=Invocation(
            package_path=root, target="claude-code", out_path=tmp_path / "out"
        ),
        manifest=manifest or {"package_name": "demo_mellea"},
        package_source_dir=root,
        python_package_dir=pkg_dir,
        supporting_asset_dirs=[],
        entry_module="pipeline",
        sig=ParsedSignature(
            function_name="run_pipeline", params=[], return_type="str", pattern="no_args"
        ),
    )


@pytest.mark.parametrize("target", ["claude-code", "pi"])
def test_skill_md_uses_bundled_skill_md_description(tmp_path: Path, target: str) -> None:
    out_path = tmp_path / f"weather-{target}"
    run_export(
        Invocation(
            package_path=_WEATHER_SKILL,
            target=target,
            out_path=out_path,
            force=True,
        )
    )
    skill_md = (out_path / "SKILL.md").read_text(encoding="utf-8")
    fm = _frontmatter(skill_md)
    assert fm["description"] == _EXPECTED_WEATHER_DESC
    assert _EXPECTED_WEATHER_DESC in skill_md


def test_mcp_uses_bundled_skill_md_description(tmp_path: Path) -> None:
    out_path = tmp_path / "weather-mcp"
    run_export(
        Invocation(
            package_path=_WEATHER_SKILL,
            target="mcp",
            out_path=out_path,
            force=True,
        )
    )
    server_src = (out_path / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(server_src)
    tool_fn = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert ast.get_docstring(tool_fn) == _EXPECTED_WEATHER_DESC


def test_get_skill_description_prefers_bundled_skill_md_over_manifest(tmp_path: Path) -> None:
    loaded = _make_loaded(
        tmp_path,
        manifest={
            "package_name": "demo_mellea",
            "runtime_metadata": {
                "c1_identity": {"entries": [{"description": "Stale manifest desc"}]}
            },
        },
        pkg_skill_md="---\nname: demo\ndescription: From bundled SKILL.md\n---",
    )
    assert get_skill_description(loaded) == "From bundled SKILL.md"


def test_get_skill_description_falls_back_to_source_dir_and_manifest(tmp_path: Path) -> None:
    loaded_root = _make_loaded(
        tmp_path / "case_root",
        separate_root=True,
        root_skill_md="---\nname: demo\ndescription: From root SKILL.md\n---\n\nBody\n",
    )
    assert get_skill_description(loaded_root) == "From root SKILL.md"

    loaded_manifest = _make_loaded(
        tmp_path / "case_manifest",
        pkg_skill_md="---\nname: demo\n---\nNo description in frontmatter.\n",
        manifest={
            "package_name": "demo_mellea",
            "categories_resolved": {
                "c1_identity": {"entries": [{"description": "Manifest fallback desc"}]}
            },
        },
    )
    assert get_skill_description(loaded_manifest) == "Manifest fallback desc"

    loaded_invalid_yaml = _make_loaded(
        tmp_path / "case_invalid",
        pkg_skill_md="---\ndescription: [unclosed\n---\n",
    )
    assert get_skill_description(loaded_invalid_yaml, default="Default desc") == "Default desc"


@pytest.mark.parametrize(
    "raw_desc",
    [
        'Scan URLs: check "quoted" and #hash values',
        'Has """triple quotes""" and trailing\\backslash"',
    ],
)
def test_special_chars_in_skill_md_description_produce_valid_outputs(
    tmp_path: Path, raw_desc: str
) -> None:
    pkg_dir = tmp_path / "special_mellea"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "pipeline.py").write_text("def run_pipeline() -> str:\n    return 'ok'\n", encoding="utf-8")
    (pkg_dir / "SKILL.md").write_text(
        f"---\nname: special\ndescription: {json.dumps(raw_desc)}\n---\n",
        encoding="utf-8",
    )
    (pkg_dir / "melleafy.json").write_text(
        json.dumps(
            {
                "manifest_version": "1.1.0",
                "package_name": "special_mellea",
                "modality": "synchronous_oneshot",
                "entry_signature": "run_pipeline() -> str",
            }
        ),
        encoding="utf-8",
    )

    for target in ("claude-code", "pi"):
        out_dir = tmp_path / f"out-{target}"
        run_export(
            Invocation(package_path=pkg_dir, target=target, out_path=out_dir, force=True)
        )
        fm = _frontmatter((out_dir / "SKILL.md").read_text(encoding="utf-8"))
        assert fm["description"] == raw_desc

    out_mcp = tmp_path / "out-mcp"
    run_export(
        Invocation(package_path=pkg_dir, target="mcp", out_path=out_mcp, force=True)
    )
    server_src = (out_mcp / "server.py").read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        tree = ast.parse(server_src)
    tool_fn = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    assert ast.get_docstring(tool_fn) == raw_desc


@pytest.mark.parametrize("target", ["claude-code", "pi"])
def test_run_sh_forwards_optional_args_to_python_module(tmp_path: Path, target: str) -> None:
    """run.sh should execute `python -m <package> "$@"` so optional flags and ordering work."""
    pkg_dir = tmp_path / "demo_skill_mellea"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill description.\n---\n",
        encoding="utf-8",
    )
    (pkg_dir / "pipeline.py").write_text(
        "def run_pipeline(target: str, mode: str = 'fast', verbose: bool = False) -> str:\n"
        "    return f'{target}:{mode}:{verbose}'\n",
        encoding="utf-8",
    )
    (pkg_dir / "main.py").write_text(
        "import argparse, json\n"
        "def main() -> None:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('target')\n"
        "    parser.add_argument('--mode', default='fast')\n"
        "    parser.add_argument('--verbose', action='store_true')\n"
        "    args = parser.parse_args()\n"
        "    print(json.dumps({'target': args.target, 'mode': args.mode, 'verbose': args.verbose}))\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (pkg_dir / "__main__.py").write_text(
        "from .main import main\nmain()\n",
        encoding="utf-8",
    )
    (pkg_dir / "melleafy.json").write_text(
        json.dumps(
            {
                "manifest_version": "1.1.0",
                "package_name": "demo_skill_mellea",
                "modality": "synchronous_oneshot",
                "entry_signature": "run_pipeline(target: str, mode: str = 'fast', verbose: bool = False) -> str",
            }
        ),
        encoding="utf-8",
    )

    out_path = tmp_path / f"demo-{target}"
    run_export(
        Invocation(
            package_path=pkg_dir,
            target=target,
            out_path=out_path,
            force=True,
        )
    )

    run_sh = out_path / "scripts" / "run.sh"
    run_sh_text = run_sh.read_text(encoding="utf-8")
    assert "python -m demo_skill_mellea" in run_sh_text

    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["bash", str(run_sh), "--mode", "deep", "--verbose", "hello world"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(proc.stdout) == {
        "target": "hello world",
        "mode": "deep",
        "verbose": True,
    }


def test_run_sh_with_guardian_executes_module_with_optional_args(tmp_path: Path) -> None:
    """When policy_manifest.json is present, run.sh still invokes <package>.__main__ with full argv."""
    from mellea_skills_compiler.export.targets.claude_code import _render_run_sh

    pkg_dir = tmp_path / "guarded_mellea"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "__main__.py").write_text(
        "import argparse, json\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('query')\n"
        "parser.add_argument('--flag', default='off')\n"
        "args = parser.parse_args()\n"
        "print(json.dumps({'query': args.query, 'flag': args.flag}))\n",
        encoding="utf-8",
    )

    # Without an on-disk policy_manifest.json at runtime, the guardian snippet skips
    # plugin initialization while still exercising the runpy.run_module wrapper.
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    run_sh = scripts_dir / "run.sh"
    run_sh.write_text(
        _render_run_sh(
            modality="synchronous_oneshot",
            package_name="guarded_mellea",
            export_version="0.1.0",
            has_policy_manifest=True,
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{Path(sys.executable).parent}:{env.get('PATH', '')}"
    proc = subprocess.run(
        ["bash", str(run_sh), "--flag", "on", "test input"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(proc.stdout) == {"query": "test input", "flag": "on"}
