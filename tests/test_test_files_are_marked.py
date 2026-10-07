"""Guard: no test file may be silently deselected by the unit gate.

The gate is `pytest tests/ -m unit`. A test file that carries no unit marker is
DESELECTED, not failed - so it can sit in the tree passing forever while never
running. That is not hypothetical: it happened to this project. Six test files
added in Waves 1-3, including the divergence suite that guards the single most
important behaviour in the voice pipeline, were never executed by the gate; the
suite reported green the whole time.

This test makes the hole visible. Files that came from upstream and were already
deselected by the upstream gate are allowed via the recorded list below; ANY new
unmarked file fails, with instructions.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TESTS_DIR = Path(__file__).resolve().parent

#: Test files inherited from upstream that carry no unit marker. Upstream's own
#: gate already deselected these, so leaving them unmarked preserves the baseline
#: exactly. Do NOT add to this list: mark the new file instead.
PRE_EXISTING_UNMARKED = ['test_compound_query.py', 'test_config_atomic_save.py', 'test_config_path_expansion.py', 'test_desktop_keepalive_workers.py', 'test_diary_enrichment_flow.py', 'test_diary_poisoning_defence.py', 'test_diary_rewrite_sweep.py', 'test_diary_topic_optimise.py', 'test_embedding_timeout_wiring.py', 'test_engine_planner_integration.py', 'test_engine_tool_search_loop.py', 'test_enrichment.py', 'test_eval_helpers.py', 'test_factory_dispatch_wiring.py', 'test_frozen_env_guards.py', 'test_generate_icons.py', 'test_intent_judge.py', 'test_listening_ux_overhaul.py', 'test_llm_arguments_encoding.py', 'test_llm_backend.py', 'test_llm_factory.py', 'test_llm_thinking.py', 'test_location_context.py', 'test_mcp_catalogue.py', 'test_mcp_e2e.py', 'test_mcp_integration.py', 'test_missing_portaudio.py', 'test_model_tiers.py', 'test_openai_compatible_e2e.py', 'test_planner.py', 'test_portaudio_serialisation.py', 'test_prompt_dump.py', 'test_prompts.py', 'test_provider_model_resolution.py', 'test_query_validation.py', 'test_settings_window.py', 'test_setup_wizard.py', 'test_setup_wizard_install_chain.py', 'test_short_query_echo.py', 'test_splash_screen.py', 'test_state_manager.py', 'test_system_prompt.py', 'test_text_tool_call_parser.py', 'test_time_context.py', 'test_tool_search_tool.py', 'test_transcript_buffer.py', 'test_tts_preprocessing.py', 'test_vram.py', 'test_wizard_layout.py', 'tools/builtin/nutrition/test_delete_meal.py', 'tools/builtin/nutrition/test_fetch_meals.py', 'tools/builtin/nutrition/test_log_meal.py', 'tools/builtin/test_fetch_web_page.py', 'tools/builtin/test_local_files.py', 'tools/builtin/test_screenshot.py', 'tools/builtin/test_stop.py', 'tools/builtin/test_time_tool.py', 'tools/builtin/test_weather.py', 'tools/builtin/test_web_search.py']


def _unmarked_files():
    found = set()
    for path in sorted(TESTS_DIR.rglob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "pytest.mark.unit" in text or "pytestmark" in text:
            continue
        found.add(path.relative_to(TESTS_DIR).as_posix())
    return found


def test_no_new_unmarked_test_files():
    """Every test file we add must be marked, or the gate skips it in silence."""
    unmarked = _unmarked_files()
    new_unmarked = sorted(unmarked - set(PRE_EXISTING_UNMARKED))
    assert not new_unmarked, (
        "These test files have no unit marker, so `pytest -m unit` will DESELECT "
        "them and they will never run:\n  "
        + "\n  ".join(new_unmarked)
        + "\n\nAdd `pytestmark = pytest.mark.unit` near the imports (or mark each "
        "test). If the file is genuinely an integration/e2e/performance suite, mark "
        "it with the right marker instead so it is deselected on purpose."
    )


def test_the_recorded_baseline_is_still_accurate():
    """Tighten the record when upstream files get marked; never let it rot."""
    unmarked = _unmarked_files()
    stale = sorted(set(PRE_EXISTING_UNMARKED) - unmarked)
    assert not stale, (
        "These files are recorded as unmarked but now carry a marker; remove them "
        "from PRE_EXISTING_UNMARKED:\n  " + "\n  ".join(stale)
    )
