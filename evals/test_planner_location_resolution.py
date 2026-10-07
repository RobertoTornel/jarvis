"""Dependent time calls preserve punctuation in discovered place names."""
import pytest

from evals.helpers import voice_config
from evals.tool_routing import requires_judge_llm
from claire.reply.planner import resolve_next_tool_call
from claire.tools.builtin.time_tool import TimeTool

pytestmark = [pytest.mark.eval, requires_judge_llm]


@pytest.mark.parametrize('place', ['St.Gallen', 'St.Albans', 'St.Louis'])
def test_time_resolver_preserves_discovered_place(place):
    schema = [{'type': 'function', 'function': {
        'name': 'getTime',
        'description': 'Get the current time for the specified place.',
        'parameters': TimeTool().inputSchema,
    }}]
    resolved = resolve_next_tool_call(
        voice_config(), "getTime location='<discovered city>'",
        [('localLookup', '{}', f'The selected city is {place}.')],
        schema, timeout_sec=60.0,
    )
    assert resolved is not None, '🕐 A discovered city must remain resolvable'
    name, arguments = resolved
    assert name == 'getTime', f'🕐 Time tool was changed: {resolved}'
    assert arguments.get('location', '').casefold() == place.casefold(), f'📍 City was altered: {resolved}'
