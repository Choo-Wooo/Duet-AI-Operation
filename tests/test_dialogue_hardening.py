"""B2-5/6: real text/file parsing, no mocked parser."""
from pathlib import Path
import pytest
from duet.core.dialogue import Dialogue, extract_directives, HEADER_RE, DIRECTIVE_RE, control_text
from duet.core.textutil import extract_memory


def test_task_preserves_fenced_argument_bytes():
    arg = 'do this\r\n```python\r\nprint(1)\r\n```\r\nend'
    assert extract_directives('<!-- duet: TASK ' + arg + ' -->') == [('TASK', arg)]


def test_dialogue_tests_do_not_read_live_workspace(monkeypatch, tmp_path):
    original = Path.read_text
    live = Path(__file__).resolve().parents[2] / 'DIALOGUE.md'
    def read(path, *args, **kwargs):
        assert path != live, 'tests must use the frozen fixture'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    test_real_dialogue_copy_keeps_turns_and_controls(tmp_path)
    test_real_turn_30_inline_examples_leave_only_rework(tmp_path)


@pytest.mark.parametrize('fence', ['```', '~~~~', '````'])
def test_headers_ignore_fences(tmp_path, fence):
    text = f'## [human] #1\n{fence}text\n## [fake] #99\n<!-- duet: CANCEL bad -->\n{fence}\n## [architect] #2\nhello'
    d = Dialogue(tmp_path)
    d.path.write_text(text)
    assert [t.n for t in d.turns()] == [1, 2]
    assert '## [fake] #99' in d.turns()[0].body


@pytest.mark.parametrize('text', ['text <!-- duet: CANCEL bad -->', '`<!-- duet: CANCEL bad -->`',
                                 '`span\n<!-- duet: CANCEL bad -->\nend`'])
def test_directives_are_line_anchored_outside_inline_code(text):
    assert extract_directives(text) == []


def test_multiline_task_with_inline_files_label():
    text = '  <!-- duet: TASK first\nuse ```files label\nlast -->\n<!-- duet: PLAN ready -->'
    assert extract_directives(text) == [('TASK', 'first\nuse ```files label\nlast'), ('PLAN', 'ready')]


def test_unclosed_inline_marker_does_not_span_paragraphs():
    text = 'label ```files\n\n<!-- duet: AGREE v1 -->\n\nnext ```files'
    assert extract_directives(text) == [('AGREE', 'v1')]


def test_real_dialogue_copy_keeps_turns_and_controls(tmp_path):
    source = Path(__file__).parent / 'fixtures/dialogue_1_32.md'
    text = source.read_text()
    expected_numbers = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16,
                        17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32]
    d = Dialogue(tmp_path)
    d.path.write_text(text)
    assert [t.n for t in d.turns()] == expected_numbers
    assert sum(len(t.directives) for t in d.turns()) == 30


@pytest.mark.parametrize('fence', ['```', '~~~'])
@pytest.mark.parametrize('second_has_code', [False, True])
def test_unclosed_fence_recovers_next_turn_and_directives(tmp_path, fence, second_has_code):
    """R1–R3: only the sequential header recovers a truncated fenced turn."""
    second_code = f'{fence}python\n## [example] #99\n<!-- duet: CANCEL example -->\n{fence}\n' if second_has_code else ''
    text = (f'## [architect] #1\n{fence}python\nprint(1)\n'
            '## [example] #99\n<!-- duet: CANCEL hidden -->\n'
            '## [implementer] #2\n' + second_code + '<!-- duet: REPORT done -->\n'
            '## [architect] #3\n<!-- duet: STATUS done -->\n')
    d = Dialogue(tmp_path); d.path.write_text(text)
    turns = d.turns()
    assert [t.n for t in turns] == [1, 2, 3]
    assert [t.directives for t in turns] == [[], [('REPORT', 'done')], [('STATUS', 'done')]]
    assert d.max_number() == 3
    assert d.find('implementer', 2).n == 2
    assert text[turns[1].start:turns[1].end].startswith('## [implementer] #2\n')
    assert '## [example] #99' in turns[0].body


def test_real_turn_30_inline_examples_leave_only_rework(tmp_path):
    """R5: actual shared record, copied read-only; inline examples are inert."""
    source = Path(__file__).parent / 'fixtures/dialogue_1_32.md'
    d = Dialogue(tmp_path); d.path.write_text(source.read_text())
    turn = d.get(30)
    assert turn is not None
    assert len(turn.directives) == 1
    assert turn.directives[0][0] == 'REWORK'


@pytest.mark.parametrize('nl', ['\n', '\r\n'])
@pytest.mark.parametrize('inner', ['```python', '~~~python'])
def test_memory_nested_fence_and_crlf(nl, inner):
    close = inner[:3]
    body = f'first\n{inner}\nprint(1)\n{close}\nlast'
    text = ('answer\n```duet-memory\n'+body+'\n```\n').replace('\n', nl)
    clean, memory = extract_memory(text)
    assert clean == 'answer'
    assert memory.replace('\r\n', '\n') == body


@pytest.mark.parametrize('text', ['````text\n```duet-memory\nexample\n```\n````',
                                  '> ```duet-memory\n> example\n> ```',
                                  '```duet-memory\nincomplete'])
def test_memory_examples_and_incomplete_are_preserved(text):
    assert extract_memory(text) == (text, None)


def test_memory_last_complete_block():
    clean, body = extract_memory('```duet-memory\nold\n```\nanswer\n```duet-memory\nnew\n```')
    assert clean.strip() == 'answer'
    assert body == 'new'
