"""A real max-turn digest admits incompletion and preserves recorded findings."""
import pytest

from evals.helpers import voice_config
from evals.tool_routing import requires_judge_llm
from claire.reply.enrichment import digest_loop_for_max_turns

pytestmark = [pytest.mark.eval, requires_judge_llm]
CASES = [
    ('Check the current Tokyo time and date.', 'getTime',
     '{"location":"Tokyo","local_time":"02:42","local_date":"2026-04-21","timezone":"Asia/Tokyo"}',
     ('tokyo', '2026-04-21'), ('could not', "couldn't", 'not fully', 'unable', 'not complete', 'ran out')),
    ('Find dinner ideas matching my diet and check nearby restaurant opening times.', 'searchMemory',
     '{"diet":"vegetarian","food_preferences":["ramen","Thai curry"]}',
     ('vegetarian', 'ramen'), ('could not', "couldn't", 'not fully', 'unable', 'not complete', 'ran out')),
    ("Tokyo'da saat kaç?", 'getTime',
     '{"location":"Tokyo","local_time":"02:42","local_date":"2026-04-21","timezone":"Asia/Tokyo"}',
     ('2026-04-21',), ('bitiremed', 'tamamlayamad', 'getiremed', 'tamamlanamad', 'sağlayamad')),
]


@pytest.mark.parametrize('query, name, data, facts, caveats', CASES)
def test_max_turn_digest_preserves_partial_findings(query, name, data, facts, caveats):
    cfg = voice_config()
    messages = [
        {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': name, 'arguments': {}}}]},
        {'role': 'tool', 'name': name, 'content': data},
    ]
    result = digest_loop_for_max_turns(query, messages, cfg)
    assert result and result.strip(), '⏳ Max-turn digest must produce a usable partial reply'
    lowered = result.casefold()
    assert all(fact in lowered for fact in facts), f'📋 Recorded findings are missing: {result}'
    opening = lowered.split('.', 1)[0].replace('’', "'")
    assert any(caveat in opening for caveat in caveats), f'⏳ Opening incompletion caveat is missing: {result}'
