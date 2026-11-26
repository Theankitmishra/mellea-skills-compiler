def test_render_run_sh_synchronous_oneshot_calls_python_module():
    """run.sh must delegate to `python -m <package> "$@"` so all positional and
    optional CLI arguments are parsed by the bundled package itself."""
    from mellea_skills_compiler.export.targets.claude_code import _render_run_sh

    result = _render_run_sh(
        modality="synchronous_oneshot",
        package_name="clawdefender_mellea",
        entry_module="pipeline",
        entry_function="run_pipeline",
        pattern="single_positional",
        params=[
            {"name": "input_text", "type": "str", "required": True, "default": None},
            {"name": "check_mode", "type": "str", "required": False, "default": '"validate"'},
        ],
        export_version="0.1.0",
    )
    assert 'exec python -m clawdefender_mellea "$@"' in result
    assert "sys.argv[2]" not in result


def test_render_run_sh_streaming_calls_unbuffered_python_module():
    from mellea_skills_compiler.export.targets.claude_code import _render_run_sh

    result = _render_run_sh(
        modality="streaming",
        package_name="weather_mellea",
        entry_module="pipeline",
        entry_function="run_pipeline",
        pattern="single_positional",
        params=[{"name": "query", "type": "str", "required": True, "default": None}],
        export_version="0.1.0",
    )
    assert 'exec python -u -m weather_mellea "$@"' in result
    assert "sys.argv[2]" not in result
