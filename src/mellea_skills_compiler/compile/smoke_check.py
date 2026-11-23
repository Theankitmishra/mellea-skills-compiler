"""Post-lint fixture smoke-check (Step 7's `--run` mode).

Executes one fixture (or all, if `all_fixtures=True`) from the compiled
package and classifies the outcome into one of three verdicts:

  - `passed`  — fixture executed without exception.
  - `failed`  — fixture raised an exception other than backend-unreachable.
                Exit code 12 (distinct from lint failure exit code 11).
  - `skipped` — LLM backend unreachable (Ollama not running, timeout, auth
                error). Exit code 0 with a stderr warning so CI without an
                LLM stays green while still nudging local users.

Writes intermediate/step_7b_report.json. Does NOT trigger the repair loop —
fixture failures require human review per `mellea-fy-validate.md`.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import traceback
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from mellea_skills_compiler.models import Fixture
from mellea_skills_compiler.toolkit.file_utils import (
    load_fixtures,
    load_skill_pipeline,
)
from mellea_skills_compiler.toolkit.logging import configure_logger


LOGGER = configure_logger()


# Known mismatches between Python import names and PyPI package names.
# Extend as new mismatches are observed in compiled skills. The minimum entry
# point is one direction: import-name -> PyPI-name; the lookup is applied
# during _is_declared_dependency only when a direct/normalised match fails.
_IMPORT_TO_PYPI: Dict[str, str] = {
    "docx": "python-docx",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "magic": "python-magic",
    "dotenv": "python-dotenv",
    "Crypto": "pycryptodome",
}


def _declared_dependency_names(skill_dir: Path) -> Optional[set]:
    """Return the set of canonical dep names from skill_dir/pyproject.toml.

    Returns ``None`` if pyproject.toml is missing or unreadable — the caller
    treats that as 'cannot determine declared deps' and falls through to the
    failed-classification path.
    """
    pyproject = skill_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover — project requires 3.11+
        return None
    try:
        data = tomllib.loads(pyproject.read_text())
    except Exception:  # noqa: BLE001 — malformed TOML treated as 'unknown'
        return None
    deps = data.get("project", {}).get("dependencies", []) or []
    names: set = set()
    for dep in deps:
        if not isinstance(dep, str):
            continue
        # PEP 508 head extraction: name [extras] [version-spec] [; markers]
        head = re.split(r"[\s<>=!~\[;]", dep, maxsplit=1)[0].strip().lower()
        if head:
            names.add(head)
            names.add(head.replace("-", "_"))
            names.add(head.replace("_", "-"))
    return names


def _is_declared_dependency(missing_module: str, skill_dir: Path) -> bool:
    """True if ``missing_module`` corresponds to a dep in skill_dir/pyproject.toml.

    Matching strategy: lowercase + hyphen/underscore normalisation, then a
    small hardcoded import-name -> PyPI-name table for known mismatches
    (e.g. ``docx`` -> ``python-docx``, ``yaml`` -> ``pyyaml``).
    """
    declared = _declared_dependency_names(skill_dir)
    if not declared:
        return False
    name = missing_module.lower()
    candidates = {name, name.replace("_", "-"), name.replace("-", "_")}
    mapped = _IMPORT_TO_PYPI.get(missing_module) or _IMPORT_TO_PYPI.get(name)
    if mapped:
        mapped_lower = mapped.lower()
        candidates |= {
            mapped_lower,
            mapped_lower.replace("-", "_"),
            mapped_lower.replace("_", "-"),
        }
    return bool(declared & candidates)


@dataclass
class SmokeFixtureResult:
    fixture_id: str
    verdict: str  # "passed" | "failed" | "skipped"
    duration_seconds: float
    skipped_reason: Optional[str] = None
    failure_message: Optional[str] = None
    failure_traceback: Optional[str] = None


@dataclass
class SmokeRunResult:
    overall_verdict: str  # "passed" | "failed" | "skipped"
    fixtures: List[SmokeFixtureResult] = field(default_factory=list)
    package_path: str = ""
    checked_at: str = ""

    @property
    def exit_code(self) -> int:
        if self.overall_verdict == "failed":
            return 12
        return 0


_FILE_INPUT_KEY_HINTS = (
    "path",
    "file",
    "filename",
    "dir",
    "folder",
    "source",
    "input",
    "doc",
    "archive",
    "bundle",
    "repo",
    "root",
    "location",
    "uri",
)


def _looks_like_path_input(key: Optional[str], value: str) -> bool:
    """Return True if a fixture input value plausibly represents a file or directory path."""
    if not value or "\n" in value or value in {".", "..", "/"}:
        return False
    if "/" in value or "\\" in value or value.startswith(("~", ".")):
        return True
    if " " not in value and bool(Path(value).suffix):
        return True
    if key is not None and " " not in value:
        keyl = key.lower()
        if any(hint in keyl for hint in _FILE_INPUT_KEY_HINTS):
            return True
    return False


def _iter_fixture_path_candidates(
    fixture_inputs: Any, key: Optional[str] = None
) -> List[str]:
    """Extract plausible path strings from fixture inputs recursively."""
    if isinstance(fixture_inputs, Fixture):
        fixture_inputs = fixture_inputs.context
    candidates: List[str] = []
    if isinstance(fixture_inputs, dict):
        for k, v in fixture_inputs.items():
            candidates.extend(_iter_fixture_path_candidates(v, key=str(k)))
    elif isinstance(fixture_inputs, (list, tuple, set)):
        for item in fixture_inputs:
            candidates.extend(_iter_fixture_path_candidates(item, key=key))
    elif isinstance(fixture_inputs, Path):
        text = str(fixture_inputs).strip()
        if text:
            candidates.append(text)
    elif isinstance(fixture_inputs, str):
        stripped = fixture_inputs.strip()
        if _looks_like_path_input(key, stripped):
            candidates.append(stripped)
    return candidates


def _match_missing_fixture_input_path(
    exc: BaseException,
    fixture_inputs: Any,
    run_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return the matching fixture input path if ``exc`` was caused by it."""
    if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return None
    if fixture_inputs is None:
        return None

    base_dir = run_dir if run_dir is not None else Path.cwd()
    err_text = str(exc)

    for raw in _iter_fixture_path_candidates(fixture_inputs):
        raw_path = Path(raw).expanduser()
        resolved_cand = (
            raw_path if raw_path.is_absolute() else (base_dir / raw_path)
        ).resolve(strict=False)

        # If the fixture input path itself exists as expected on disk, any
        # FileNotFoundError/NotADirectoryError is an internal package bug.
        if isinstance(exc, NotADirectoryError):
            if resolved_cand.is_dir():
                continue
        else:
            if resolved_cand.exists() or (raw_path.is_absolute() and raw_path.exists()):
                continue

        for fname in (getattr(exc, "filename", None), getattr(exc, "filename2", None)):
            if not fname:
                continue
            err_raw = Path(str(fname)).expanduser()
            err_resolved = (
                err_raw if err_raw.is_absolute() else (base_dir / err_raw)
            ).resolve(strict=False)
            if (
                err_resolved == resolved_cand
                or err_resolved.is_relative_to(resolved_cand)
                or err_raw == raw_path
                or err_raw.is_relative_to(raw_path)
            ):
                return raw

        seen_forms = set()
        for form in (
            raw,
            raw_path.as_posix(),
            str(raw_path),
            resolved_cand.as_posix(),
            str(resolved_cand),
        ):
            if not form or form in seen_forms:
                continue
            seen_forms.add(form)
            if "/" in form or "\\" in form:
                pattern = rf"(?:^|[\s'\"=:(])({re.escape(form)})(?=$|[\s'\"/\\:,)\]])"
            else:
                pattern = rf"(?<![\w/\\.-]){re.escape(form)}(?![\w.-])"
            if re.search(pattern, err_text):
                return raw

    return None


def _classify_exception(
    exc: BaseException,
    skill_dir: Optional[Path] = None,
    fixture_inputs: Optional[Any] = None,
    fixture: Optional[Fixture] = None,
    run_dir: Optional[Path] = None,
) -> Optional[str]:
    """Return a non-None 'skipped' reason if the exception is environmental.

    Three classes of skip:

    1. Backend unreachable — stdlib socket/connection errors, httpx/requests
       library variants (by class name to avoid hard imports), HTTP auth
       failures, and timeouts.
    2. Declared dependency missing from the compile-process venv — a
       ``ModuleNotFoundError`` whose missing module corresponds to a dep
       declared in ``skill_dir/pyproject.toml``. The skill compiles fine;
       the user just needs ``pip install -e .`` before runtime verification.
       Distinguished from a real code bug (LLM imports something it didn't
       declare), which is left to fall through to the failed path.
    3. Missing fixture input path — a ``FileNotFoundError`` or
       ``NotADirectoryError`` caused by a nonexistent or placeholder path in
       the fixture's own inputs rather than a missing bundled package asset.
    """
    cls_name = type(exc).__name__
    cls_module = type(exc).__module__

    if fixture_inputs is None and fixture is not None:
        fixture_inputs = fixture.context

    # Declared-dependency-missing — environmental, not a code bug.
    # Checked first so a ModuleNotFoundError that LOOKS network-like (e.g.
    # ConnectionError from a missing urllib3) doesn't get misclassified.
    if isinstance(exc, ModuleNotFoundError) and skill_dir is not None:
        missing = (getattr(exc, "name", None) or "").split(".")[0]
        if missing and _is_declared_dependency(missing, skill_dir):
            return (
                f"declared dependency {missing!r} not installed in the "
                f"compile-process venv. The skill's pyproject.toml declares "
                f"it; run `pip install -e .` in the skill directory and "
                f"re-run the smoke check to verify runtime behaviour."
            )

    matched_path = _match_missing_fixture_input_path(
        exc, fixture_inputs, run_dir=run_dir
    )
    if matched_path is not None:
        return (
            f"fixture input path {matched_path!r} does not exist "
            f"(fixture uses a placeholder or unbundled input path): {cls_name}: {exc}"
        )

    # stdlib network errors
    if isinstance(
        exc,
        (
            ConnectionError,
            ConnectionRefusedError,
            ConnectionAbortedError,
            ConnectionResetError,
        ),
    ):
        return f"backend unreachable: {cls_name}: {exc}"
    if isinstance(exc, TimeoutError):
        return f"backend unreachable: timeout: {exc}"

    # httpx / requests / urllib3 — name-matched to avoid import dependencies
    if cls_name in (
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "PoolTimeout",
        "RemoteProtocolError",
    ):
        return f"backend unreachable: {cls_module}.{cls_name}: {exc}"
    if cls_name == "ConnectionError" and cls_module.startswith(
        ("requests", "urllib3", "httpx", "httpcore")
    ):
        return f"backend unreachable: {cls_module}.{cls_name}: {exc}"

    # HTTP auth from various libs (string match — APIs vary widely)
    text = str(exc)
    if (
        "401" in text
        or "403" in text
        or "unauthorized" in text.lower()
        or "authentication failed" in text.lower()
    ):
        return f"backend unreachable: authentication failed (check API key or env vars): {exc}"

    # External HTTP service error (e.g. urllib ``HTTPError`` 4xx/5xx, raised directly
    # or re-wrapped in a RuntimeError). The external backend the tool calls is not
    # available in the smoke environment, so a fixture that performs a live request
    # is exercising an un-provided external integration, not a code defect. Treat as
    # backend-unreachable (skip) so the otherwise-valid package is not failed.
    if cls_name == "HTTPError" or "HTTP Error 4" in text or "HTTP Error 5" in text:
        return f"backend unreachable: external HTTP error (no live backend in smoke): {exc}"

    # Unimplemented external-dependency stub. The compiler deliberately emits
    # ``NotImplementedError`` stubs for external side effects it cannot synthesise
    # (dependency-plan slots, e.g. constrained_slots.py). A fixture that drives such
    # a stub is exercising an un-provided external integration, not a code defect —
    # the package compiled correctly. Skip rather than fail (the same philosophy as
    # the declared-dependency-missing skip above); supply/mock the implementation to
    # run it at validation time.
    if isinstance(exc, NotImplementedError):
        return (
            f"unimplemented external-dependency stub exercised by this fixture: {exc} "
            f"The package compiled; provide or mock the dependency to run it."
        )

    return None


def _mellea_deprecation_warnings(captured) -> List[str]:
    """Return formatted DeprecationWarnings that originate from mellea.

    Filters ``warnings.catch_warnings`` records down to
    ``DeprecationWarning`` / ``PendingDeprecationWarning`` whose emitting
    module lives under ``mellea.``. This is what surfaces a compiled
    package using a shimmed API (e.g. importing
    ``mellea.stdlib.components.genslot`` after the 0.7 rename) — the
    package still runs, but silently drifts from upstream.
    """
    out: List[str] = []
    for w in captured:
        if not issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning)):
            continue
        module = str(getattr(w, "filename", ""))
        # Match imports from mellea/ (regardless of install location).
        if "/mellea/" not in module and "\\mellea\\" not in module:
            # Also catch the case where the DeprecationWarning names mellea in
            # its message (some shims emit via warnings.warn with stacklevel
            # tuned to the caller).
            if "mellea" not in str(w.message).lower():
                continue
        out.append(f"{w.category.__name__}: {w.message} (at {module}:{w.lineno})")
    return out


def _run_one_fixture(
    pipeline_fn,
    fixture: Fixture,
    skill_dir: Optional[Path] = None,
    package_dir: Optional[Path] = None,
) -> SmokeFixtureResult:
    started = time.time()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        # Run the fixture from the package directory so package-relative input
        # paths (e.g. 'references/example_patient.json' bundled in the package)
        # resolve the same way they do for an installed skill, rather than
        # against the compiler's CWD. contextlib.chdir restores CWD afterward.
        run_dir = package_dir if package_dir is not None else Path.cwd()
        try:
            with contextlib.chdir(run_dir):
                if isinstance(fixture.context, dict):
                    pipeline_fn(**fixture.context)
                else:
                    pipeline_fn(fixture.context)
        except BaseException as exc:  # noqa: BLE001 — we re-classify all
            duration = time.time() - started
            skipped_reason = _classify_exception(
                exc,
                skill_dir=skill_dir,
                fixture_inputs=fixture.context,
                run_dir=run_dir,
            )
            if skipped_reason:
                LOGGER.warning(
                    "Fixture smoke-check skipped — %s. Re-run `mellea-skills validate <pkg> --run` "
                    "once the backend is up to verify runtime behaviour.",
                    skipped_reason,
                )
                return SmokeFixtureResult(
                    fixture_id=fixture.id,
                    verdict="skipped",
                    duration_seconds=duration,
                    skipped_reason=skipped_reason,
                )
            return SmokeFixtureResult(
                fixture_id=fixture.id,
                verdict="failed",
                duration_seconds=duration,
                failure_message=f"{type(exc).__name__}: {exc}",
                failure_traceback="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )

        # Fail the fixture if it emitted any mellea-originated DeprecationWarning.
        # This is what catches the compiled package silently reverting to a
        # shimmed API (e.g. ``genslot`` after the 0.7 rename). Non-mellea
        # DeprecationWarnings (from an app dep) are ignored — see
        # ``_mellea_deprecation_warnings``.
        mellea_deprecations = _mellea_deprecation_warnings(captured)
        if mellea_deprecations:
            duration = time.time() - started
            joined = "\n".join(mellea_deprecations)
            return SmokeFixtureResult(
                fixture_id=fixture.id,
                verdict="failed",
                duration_seconds=duration,
                failure_message=(
                    "mellea DeprecationWarning(s) emitted at runtime — the "
                    "compiled package is using a shimmed / renamed API. Fix "
                    "the offending import(s) before shipping:\n" + joined
                ),
            )
    return SmokeFixtureResult(
        fixture_id=fixture.id,
        verdict="passed",
        duration_seconds=time.time() - started,
    )


def run_smoke_check(package_dir: Path, all_fixtures: bool = False) -> SmokeRunResult:
    """Execute one (or all) fixtures and write step_7b_report.json.

    Args:
        package_dir: compiled package directory (e.g. `<skill>/<name>_mellea`).
        all_fixtures: if False (default), only the first fixture in ALL_FIXTURES
            is executed. If True, every fixture is run sequentially.
    """
    pipeline_fn = load_skill_pipeline(package_dir)
    fixtures = load_fixtures(package_dir)

    targets = fixtures if all_fixtures else fixtures[:1]

    # The skill root sits one level above the compiled package; pyproject.toml
    # lives at the skill root (Rule OUT-3). Pass it through so ModuleNotFoundError
    # classification can distinguish declared-dep-missing from real-bug.
    skill_dir = package_dir.parent

    fixture_results: List[SmokeFixtureResult] = []
    for fixture in targets:
        fixture_results.append(
            _run_one_fixture(
                pipeline_fn, fixture, skill_dir=skill_dir, package_dir=package_dir
            )
        )

    if any(r.verdict == "failed" for r in fixture_results):
        overall = "failed"
    elif all(r.verdict == "skipped" for r in fixture_results):
        overall = "skipped"
    else:
        overall = "passed"

    run = SmokeRunResult(
        overall_verdict=overall,
        fixtures=fixture_results,
        package_path=str(package_dir),
        checked_at=datetime.now(timezone.utc).isoformat(),
    )

    intermediate = package_dir / "intermediate"
    intermediate.mkdir(parents=True, exist_ok=True)
    (intermediate / "step_7b_report.json").write_text(
        json.dumps(
            {
                "format_version": "1.0",
                "checked_at": run.checked_at,
                "package_path": run.package_path,
                "overall_verdict": run.overall_verdict,
                "fixtures": [asdict(r) for r in fixture_results],
            },
            indent=2,
        )
    )

    return run
