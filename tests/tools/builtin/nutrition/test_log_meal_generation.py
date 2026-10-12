"""Meal extraction and coaching can emit answers after local reasoning."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claire.memory.db import Database
from claire.tools.builtin.nutrition import log_meal

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('provider', ['ollama', 'openai_compatible'])
@pytest.mark.parametrize('phase', ['extraction', 'coaching'])
def test_nutrition_answer_survives_reasoning_budget(monkeypatch, provider, phase):
    cfg = SimpleNamespace(
        llm_provider=provider, llm_base_url='http://127.0.0.1:1/v1',
        ollama_base_url='http://127.0.0.1:1', llm_chat_model='local-reasoning-model',
        llm_chat_timeout_sec=7.3, llm_thinking_enabled=False,
    )
    meal = {'description': 'eggs with toast', 'calories_kcal': 220,
            'protein_g': 14, 'carbs_g': 18, 'fat_g': 11, 'confidence': 0.8}
    coaching = 'Drink water and add vegetables to your next meal.'
    reasoning = ' '.join(['Consider the meal and typical portions.'] * 80)
    observed_timeouts = []
    def post(url, **kwargs):
        observed_timeouts.append(kwargs['timeout'])
        payload = kwargs['json']
        is_extraction = payload['messages'][0]['content'] == log_meal.NUTRITION_SYS
        answer = json.dumps(meal) if is_extraction else coaching
        cap = payload.get('max_tokens', payload.get('options', {}).get('num_predict', 0))
        required = len(reasoning.split()) + len(answer.split())
        response = MagicMock()
        response.__enter__.return_value = response
        message = {'content': answer if cap >= required else '', 'reasoning_content': reasoning}
        response.json.return_value = ({'message': message} if provider == 'ollama'
                                     else {'choices': [{'message': message}]})
        return response
    monkeypatch.setattr('requests.post', post)
    if phase == 'extraction':
        db = Database(':memory:', sqlite_vss_path=None)
        try:
            reply = log_meal.extract_and_log_meal(db, cfg, 'I ate eggs with toast', 'stdin')
            now = datetime.now(timezone.utc)
            rows = db.get_meals_between((now - timedelta(minutes=1)).isoformat(),
                                        (now + timedelta(minutes=1)).isoformat())
            assert rows and rows[0]['description'] == meal['description']
            assert rows[0]['calories_kcal'] == meal['calories_kcal']
            assert reply and meal['description'] in reply
        finally:
            db.close()
    else:
        assert log_meal.generate_followups_for_meal(cfg, meal['description'], 'approximate macros') == coaching
    assert observed_timeouts and all(timeout == cfg.llm_chat_timeout_sec for timeout in observed_timeouts)


def _tmp_db(tmp_path):
    return Database(str(tmp_path / "nutrition-probe.db"), None)


def _reasoning_post(monkeypatch, log_meal, reasoning_tokens, answer, seen):
    """Fake the gateway: the model's reasoning is charged against max_tokens,
    so the answer arrives only when the budget covers reasoning + answer.

    Only extraction calls are recorded in ``seen``; the coaching follow-up is
    a second, separate LLM call and answers immediately so it cannot be
    mistaken for a retry.
    """
    def post(url, **kwargs):
        payload = kwargs['json']
        cap = payload.get('max_tokens') or 0
        response = MagicMock()
        response.__enter__.return_value = response
        if payload['messages'][0]['content'] != log_meal.NUTRITION_SYS:
            response.json.return_value = {
                'choices': [{'message': {'content': 'Drink some water.'}}]}
            return response
        seen.append(cap)
        required = reasoning_tokens + 20
        content = answer if cap >= required else ''
        response.json.return_value = {
            'choices': [{'message': {'content': content, 'reasoning_content': 'x' * 10},
                         'finish_reason': 'stop' if content else 'length'}],
            'usage': {'completion_tokens_details': {'reasoning_tokens': min(cap, reasoning_tokens)}},
        }
        return response
    monkeypatch.setattr('requests.post', post)


def _cfg():
    return SimpleNamespace(
        llm_provider='openai_compatible', llm_base_url='http://127.0.0.1:1/v1',
        ollama_base_url='http://127.0.0.1:1', llm_chat_model='local-reasoning-model',
        llm_chat_timeout_sec=7.3, llm_thinking_enabled=False,
    )


def test_a_truncated_extraction_is_retried_with_room_to_finish(monkeypatch, tmp_path):
    """The failure this fixes: a detailed meal spends the whole token budget on
    reasoning and returns EMPTY content, which the tool reported as "Failed to
    log meal" after 8-16 s. Measured 2026-10-13 on the live config: a seven-item
    breakfast returned finish_reason "length" with all 1024 tokens charged as
    reasoning, and only succeeded once given 4096.

    Retrying at the same budget cannot succeed, so the retry must raise it.
    """
    meal = {'description': 'big breakfast', 'calories_kcal': 900, 'protein_g': 40,
            'carbs_g': 70, 'fat_g': 50, 'confidence': 0.7}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=3051,
                    answer=json.dumps(meal), seen=seen)

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                            'a big breakfast', 'stdin')

    assert summary, "the meal must be logged once the retry has room to finish"
    assert seen[0] == log_meal._NUTRITION_TOKEN_BUDGET
    assert len(seen) >= 2, f"a truncated answer must be retried; budgets tried: {seen}"
    assert seen[1] > seen[0], (
        f"the retry must raise the budget, not repeat it; budgets tried: {seen}"
    )
    assert seen[1] >= 4096, (
        f"the retry budget must cover the measured reasoning; got {seen[1]}"
    )


def test_a_normal_meal_is_not_retried(monkeypatch, tmp_path):
    """The cheap path stays cheap: one call when the first answer arrives."""
    meal = {'description': 'two eggs', 'calories_kcal': 145, 'protein_g': 12,
            'carbs_g': 1, 'fat_g': 10, 'confidence': 0.9}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=40,
                    answer=json.dumps(meal), seen=seen)

    log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(), 'two eggs', 'stdin')

    assert seen == [log_meal._NUTRITION_TOKEN_BUDGET], (
        f"a first-call answer must not pay for a retry; budgets tried: {seen}"
    )


def test_a_genuine_not_a_meal_answer_is_not_retried(monkeypatch, tmp_path):
    """"NONE" is a decision, not a failure: the model answered, so there is
    nothing to buy with a bigger budget."""
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=40, answer='NONE', seen=seen)

    assert log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                         'what is the weather', 'stdin') is None
    assert seen == [log_meal._NUTRITION_TOKEN_BUDGET], (
        f"a NONE verdict must not be retried; budgets tried: {seen}"
    )


def test_a_partial_json_answer_is_retried(monkeypatch, tmp_path):
    """The second face of the same truncation: the model starts the JSON and
    is cut off mid-object, so the content is non-empty but will not parse.
    Measured 2026-10-13: a simple meal ("two eggs and a coffee") failed this
    way in 8 s with no retry, because the retry only fired on empty content.
    """
    meal = {'description': 'two eggs and a coffee', 'calories_kcal': 145,
            'protein_g': 12, 'carbs_g': 1, 'fat_g': 10, 'confidence': 0.9}
    full = json.dumps(meal)
    seen = []
    calls = {'n': 0}

    def post(url, **kwargs):
        payload = kwargs['json']
        response = MagicMock()
        response.__enter__.return_value = response
        if payload['messages'][0]['content'] != log_meal.NUTRITION_SYS:
            response.json.return_value = {'choices': [{'message': {'content': 'Drink water.'}}]}
            return response
        seen.append(payload.get('max_tokens') or 0)
        calls['n'] += 1
        # First attempt: cut off mid-object, exactly as a length-capped
        # completion arrives. Second attempt: the whole object.
        content = full[: len(full) // 2] if calls['n'] == 1 else full
        response.json.return_value = {'choices': [{'message': {'content': content}}]}
        return response

    monkeypatch.setattr('requests.post', post)

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                            'two eggs and a coffee', 'stdin')

    assert summary, "a partial answer must be retried, not reported as a failure"
    assert seen[0] == log_meal._NUTRITION_TOKEN_BUDGET
    assert len(seen) >= 2 and seen[1] > seen[0], (
        f"a partial answer must buy room on the retry; budgets tried: {seen}"
    )


def test_the_ladder_climbs_until_the_reasoning_fits(monkeypatch, tmp_path):
    """Reasoning length varies between runs for the same meal (measured 3051
    tokens on one run and 6133 on another), so one escalation step is not
    always enough. The extractor must keep climbing until the answer fits, and
    must stop as soon as it does."""
    meal = {'description': 'big breakfast', 'calories_kcal': 900, 'protein_g': 40,
            'carbs_g': 70, 'fat_g': 50, 'confidence': 0.7}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=6133,
                    answer=json.dumps(meal), seen=seen)

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                            'a big breakfast', 'stdin')

    assert summary, "the meal must be logged once a budget covers the reasoning"
    assert seen == list(log_meal._NUTRITION_BUDGET_LADDER), (
        f"the ladder must climb to the budget that fits; budgets tried: {seen}"
    )
    assert seen == sorted(seen) and len(set(seen)) == len(seen), (
        "each step must be a distinct, increasing budget"
    )


def test_an_itemised_meal_starts_at_a_rung_that_can_hold_its_reasoning(monkeypatch, tmp_path):
    """Climbing from the bottom rung cost a seven-item breakfast 109.8 s, and
    2048 was measured too small for it even with the aggregate instruction. The
    starting rung is chosen from how many foods the description names, so that
    meal begins where its measured reasoning (2112 tokens) fits and pays for
    one call instead of climbing."""
    detailed = ("I had a big breakfast: three scrambled eggs, two slices of buttered "
                "sourdough toast, four rashers of bacon, half an avocado, a handful of "
                "cherry tomatoes, a large glass of orange juice and a double espresso "
                "with oat milk")
    assert log_meal._first_budget_for("two eggs and a coffee") == \
        log_meal._NUTRITION_TOKEN_BUDGET
    assert log_meal._first_budget_for(detailed) == log_meal._NUTRITION_ITEMISED_START

    meal = {'description': 'big breakfast', 'calories_kcal': 900, 'protein_g': 40,
            'carbs_g': 70, 'fat_g': 50, 'confidence': 0.7}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=2112,
                    answer=json.dumps(meal), seen=seen)

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(), detailed, 'stdin')

    assert summary
    assert seen == [log_meal._NUTRITION_ITEMISED_START], (
        f"an itemised meal must not pay for rungs measured to fail; tried: {seen}"
    )


def test_a_simple_meal_still_gets_the_cheap_first_attempt(monkeypatch, tmp_path):
    """The cheap rung stays for the common case: a short description that the
    extractor can answer inside 1024 tokens must not be made to reason longer
    just because a bigger cap was available."""
    meal = {'description': 'two eggs and a coffee', 'calories_kcal': 145,
            'protein_g': 12, 'carbs_g': 1, 'fat_g': 10, 'confidence': 0.9}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=40,
                    answer=json.dumps(meal), seen=seen)

    log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                  'two eggs and a coffee', 'stdin')

    assert seen == [log_meal._NUTRITION_TOKEN_BUDGET], (
        f"a simple meal must start on the cheap rung; tried: {seen}"
    )


def test_coaching_is_not_requested_unless_switched_on(monkeypatch, tmp_path):
    """The second model call measured 16.6-18.0 s and returned empty content on
    two calls out of three, so the default path must not make it: the meal is
    committed to the database first and the follow-up is optional flavour."""
    meal = {'description': 'two eggs', 'calories_kcal': 145, 'protein_g': 12,
            'carbs_g': 1, 'fat_g': 10, 'confidence': 0.9}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=40,
                    answer=json.dumps(meal), seen=seen)
    asked_for_coaching = []
    monkeypatch.setattr(
        log_meal, 'generate_followups_for_meal',
        lambda *a, **kw: asked_for_coaching.append(1) or 'Drink water.',
    )

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), _cfg(),
                                            'two eggs', 'stdin')

    assert summary and 'Logged meal' in summary
    assert not asked_for_coaching, (
        "coaching must not be requested when nutrition_coaching_enabled is off"
    )


def test_coaching_is_requested_when_switched_on(monkeypatch, tmp_path):
    """The capability stays available behind the key."""
    meal = {'description': 'two eggs', 'calories_kcal': 145, 'protein_g': 12,
            'carbs_g': 1, 'fat_g': 10, 'confidence': 0.9}
    seen = []
    _reasoning_post(monkeypatch, log_meal, reasoning_tokens=40,
                    answer=json.dumps(meal), seen=seen)
    cfg = _cfg()
    cfg.nutrition_coaching_enabled = True
    monkeypatch.setattr(log_meal, 'generate_followups_for_meal',
                        lambda *a, **kw: 'Drink water and add vegetables.')

    summary = log_meal.extract_and_log_meal(_tmp_db(tmp_path), cfg, 'two eggs', 'stdin')

    assert 'Drink water and add vegetables.' in summary


def test_the_extractor_asks_for_one_aggregate_estimate():
    """The aggregate instruction is the latency fix and it must not be lost:
    asking the model to sum item by item cost a seven-item breakfast thousands
    of reasoning tokens (measured 3051 and 6133 on two runs), which the gateway
    charges against max_tokens, so the answer was truncated at 1024, 2048 and
    once at 8192. As one aggregate the same meal completed inside 4096.

    The description must still list every item, so that instruction stays.
    """
    prompt = log_meal.NUTRITION_SYS.lower()

    assert "one aggregate" in prompt, "the aggregate method must be requested"
    assert "do not enumerate" in prompt or "not enumerate" in prompt, (
        "the instruction has to forbid the per-item working that caused it"
    )
    assert "description field must list all items" in prompt, (
        "the description must still name every item, or the logged meal loses them"
    )
    assert "sum their nutritional values" not in prompt, (
        "the per-item summation instruction is what made the reasoning explode"
    )
