"""Answer guards for live time grounded through graph, diary or hot memory."""
import re

from evals.helpers import assert_not_fallback_reply


def assert_usable_answer(response: str | None, context: str) -> None:
    """A recorded tool call cannot substitute for an actual reply."""
    assert isinstance(response, str) and response.strip(), f'🗣️ {context}: no answer'
    assert_not_fallback_reply(response, context)


def assert_time_reply(response: str | None, payload: str, context: str) -> None:
    """Require the supplied local time as well as a usable answer."""
    assert_usable_answer(response, context)
    clock_times = re.findall(r'(\d{1,2}):(\d{2})', payload)
    assert clock_times, '🕐 The evaluation fixture must supply a local time'
    supplied = set()
    for hour, minute in clock_times:
        supplied.add(f'{hour}:{minute}')
        supplied.add(f'{int(hour)}:{minute}')
    assert any(
        re.search(r'(?<![\d:])' + re.escape(value) + r'(?![\d])', response)
        for value in supplied
    ), f'🕐 {context}: the reply carries no supplied local time: {response}'
