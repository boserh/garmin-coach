"""The opt-in forensic copy of every outgoing Claude request (PROMPT_DUMP_DIR).

The request body is otherwise unrecoverable — ``report_logs`` keeps the question, the
token counts and the answer, but never the ``user_content`` that was posted — so "what did
the analyst actually see?" has no answer after the fact. These cover the switch itself
(off by default, written when on, bounded, never fatal) and — the important one — an AST
sweep asserting that **every** ``messages.create`` call site in ``app/analysis`` dumps
first, so a new LLM path added later can't quietly stop being recoverable.
"""
import ast
import json
import pathlib

from app.analysis import dump
from app.core.config import settings

ANALYSIS_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "analysis"


def _dump(**kw):
    base = dict(kind="morning", model="claude-sonnet-5", system="SYS",
                user_content={"question": "як я?"}, user_id=7, max_tokens=2000)
    return dump.dump_request(**{**base, **kw})


def test_off_by_default_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROMPT_DUMP_DIR", "")
    assert _dump() is None
    assert list(tmp_path.iterdir()) == []


def test_writes_the_whole_request(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROMPT_DUMP_DIR", str(tmp_path))
    path = _dump()
    assert path is not None
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert data["kind"] == "morning"
    assert data["model"] == "claude-sonnet-5"
    assert data["system"] == "SYS"
    assert data["max_tokens"] == 2000
    assert data["user_id"] == 7
    # The point of the file: the user turn, verbatim, not a summary of it.
    assert data["user_content"] == {"question": "як я?"}
    assert "morning" in pathlib.Path(path).name


def test_prune_bounds_the_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROMPT_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PROMPT_DUMP_KEEP", 3)
    for i in range(7):
        assert _dump(user_content={"n": i}) is not None
    # Which three survive depends on mtime ordering, which can tie on a fast filesystem —
    # the guarantee under test is the bound, not the identity of the survivors.
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_keep_zero_disables_pruning(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PROMPT_DUMP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PROMPT_DUMP_KEEP", 0)
    for i in range(4):
        _dump(user_content={"n": i})
    assert len(list(tmp_path.glob("*.json"))) == 4


def test_a_broken_destination_never_breaks_the_call(monkeypatch, tmp_path):
    """Observation must never take down the call it is observing: a dump into a path that
    cannot be created returns None instead of raising into messages.create."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(settings, "PROMPT_DUMP_DIR", str(blocker / "sub"))
    assert _dump() is None


def test_every_claude_call_site_dumps_first():
    """Mirror of the OPS-11 budget sweep: walk every function in app/analysis that calls
    ``messages.create`` and assert it also calls ``dump_request``. A new completion helper
    that forgets it fails here rather than silently costing us the evidence."""
    missing = []
    for path in sorted(ANALYSIS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
            if not any(c.endswith("messages.create") for c in calls):
                continue
            if not any(c.endswith("dump_request") for c in calls):
                missing.append(f"{path.name}:{fn.name}")
    assert not missing, f"messages.create without a dump_request: {missing}"
