"""Fast, offline units of the Day 3 pipeline: schemas, prose, the script check, provenance, keys."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from eddie.llm.errors import LiveEmberDisabledError
from eddie.llm.transport import KeyProvider, take_key_fd
from eddie.pipeline import prompts, rehearsal
from eddie.pipeline.calls import build_request, interpret, make_tag, phase_tag_like
from eddie.pipeline.schemas import (
    SchemaError,
    TriageBatch,
    chapter_problems,
    parse_json,
    prose,
    validate_text_format,
)
from eddie.pipeline.scriptcheck import check_script
from eddie.sandbox.runner import SANDBOX_SRC, SandboxRunner
from eddie.state.artifacts import ArtifactStore, ProvenanceError, connect_ro
from tests.emberhelpers import make_gateway

# --- schemas and parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(prompts.SCHEMAS))
def test_every_schema_is_strict_and_valid(name):
    validate_text_format(prompts.text_format(name))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f["format"].update(strict=False),
        lambda f: f["format"]["schema"].update(additionalProperties=True),
        lambda f: f["format"]["schema"]["required"].pop(),
        lambda f: f["format"].update(name="has spaces"),
        lambda f: f["format"].update(extra=1),
    ],
)
def test_the_local_validator_refuses_what_the_server_would(mutate):
    fmt = json.loads(json.dumps(prompts.text_format("critic")))
    mutate(fmt)
    with pytest.raises(SchemaError):
        validate_text_format(fmt)


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_triage_items_are_clipped_and_matched_by_id():
    batch = TriageBatch.model_validate({"items": [
        {"id": "d1", "plain_english": " ".join(["word"] * 90), "role": "made_up", "area": "a b c d e f g h",
         "visualizability": 2, "visual_hint": "x"},
        {"id": "d9", "plain_english": "outside the batch", "role": "bridge", "area": "x", "visualizability": 0,
         "visual_hint": "none"},
    ]})
    found = batch.by_id(["d1", "d2"])
    assert list(found) == ["d1"]
    assert found["d1"].role == "structure" and len(found["d1"].plain_english.split()) <= 61


def test_prose_keeps_only_what_prose_tsx_renders():
    md = ("Intro with $\\zeta(s)$ and a [link](http://x) and *italic* and **bold**.\n### What this chapter does\nBody "
          "naming `allChi` and `madeUpName` and `x + 1`.")
    text, notes = prose(md, {"allChi"})
    assert "ζ(s)" in text and "$" not in text and "link" in text and "http" not in text
    assert "*italic*" not in text and "**bold**" in text
    assert "\n\n### What this chapter does\n\n" in text  # a heading is its own block
    assert "`allChi`" in text and "`madeUpName`" not in text and "madeUpName" in text and "`x + 1`" in text
    assert "unknown name madeUpName" in notes


def test_chapter_problems_need_the_four_sections_in_order():
    body = " ".join(["word"] * 200)
    good = "\n\n".join(f"### {s}\n\n{body}" for s in prompts.SECTIONS)
    assert chapter_problems(good) == []
    swapped = "\n\n".join(f"### {s}\n\n{body}" for s in reversed(prompts.SECTIONS))
    assert chapter_problems(swapped)
    assert chapter_problems("### What this chapter does\n\nshort")


def test_requests_carry_explicit_effort_tag_and_cache_key():
    req = build_request(phase="calibrate", structured=True, step_key="triage:x:1", attempt=2, region="R1.triage",
                        effort="low", max_output=4_096, instructions=prompts.TRIAGE,
                        parts=[{"type": "text", "text": "[d1] theorem t : True"}], schema="triage")
    assert req.effort == "low" and req.max_output_tokens == 4_096 and req.brain_region == "R1.triage"
    assert req.tag == make_tag("calibrate", "triage:x:1", 2) and len(req.tag) <= 120
    assert req.tag.startswith(phase_tag_like("calibrate").rstrip("%"))
    assert req.text_format["format"]["strict"] is True and req.prompt_cache_key.startswith("eddie:R1.triage:")
    loose = build_request(phase="calibrate", structured=False, step_key="s", attempt=0, region="R1.triage", effort="low",
                          max_output=64, instructions=prompts.TRIAGE, parts=[{"type": "text", "text": "x"}],
                          schema="triage")
    assert loose.text_format is None and "JSON Schema" in loose.instructions


@pytest.mark.parametrize("schema", sorted(prompts.SCHEMAS))
def test_rehearsal_replies_validate(schema):
    req = build_request(phase="calibrate", structured=True, step_key="s", attempt=0,
                        region={"triage": "R1.triage", "worker": "R1.worker", "critic": "R2.critic",
                                "arbiter": "R3.arbiter", "chapter": "R4.consolidator"}[schema],
                        effort={"triage": "low", "worker": "medium", "critic": "medium", "arbiter": "high",
                                "chapter": "high"}[schema],
                        max_output=4_096, instructions="x", parts=[{"type": "text", "text": "[d1] [d2] [target] theorem `t`"}],
                        schema=schema, summary="concise" if schema == "arbiter" else None)
    from eddie.llm.body import build_body
    from eddie.llm.types import EddieResponse, Usage

    text = rehearsal.respond(build_body(req))
    resp = EddieResponse("c", "mock", "m", req.brain_region, req.effort, "completed", text, Usage(1, 0, 1, 0), 0, 0, 1,
                         response_id="r")
    result = interpret(resp, schema=schema)
    assert result.ok, result.error
    if schema == "chapter":
        assert chapter_problems(result.data.synthesis_md) == []


# --- the script check --------------------------------------------------------------------------------


def test_scriptcheck_accepts_a_plain_plot():
    assert check_script(rehearsal.SCRIPT) == []


@pytest.mark.parametrize(
    "line, fragment",
    [
        ("import os", "import os"),
        ("from subprocess import run", "subprocess"),
        ("import numpy as np\nnp.ctypeslib.load_library('c', '/lib')", ".ctypeslib"),
        ("x = (1).__class__", ".__class__"),
        ("eval('1')", "eval()"),
        ("open('/etc/passwd').read()", "open()"),
        ("import matplotlib\nmatplotlib.subprocess", ".subprocess"),
    ],
)
def test_scriptcheck_names_each_problem(line, fragment):
    problems = check_script(line + "\nfig.savefig('witness.png')\n")
    assert any(fragment in p for p in problems), problems


def test_scriptcheck_needs_the_output_and_valid_syntax():
    assert any("witness.png" in p for p in check_script("print(1)\n"))
    assert any("syntax error" in p for p in check_script("def (:\n"))


# --- storage: provenance and seed safety -------------------------------------------------------------


def test_an_ember_row_needs_its_ledger_call(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    with pytest.raises(ProvenanceError):
        store.put_triage("A.t", "gloss", source="ember", call_id="c1")  # no response id
    with pytest.raises(ProvenanceError):
        store.put_chapter("slug", "T", "md", source="ember", response_id="r1")  # no call id
    store.put_triage("A.t", "gloss", source="ember", call_id="c1", response_id="r1")


def test_seed_never_overwrites_model_output(tmp_path):
    store = ArtifactStore(tmp_path / "a.sqlite")
    store.put_intuition("riemannHypothesis", "seed text", source="seed")
    store.put_intuition("riemannHypothesis", "ember text", source="ember", call_id="c", response_id="r")
    store.put_intuition("riemannHypothesis", "seed again", source="seed")  # re-running seed-demo
    store.put_chapter("main", "Main", "ember chapter", source="ember", call_id="c", response_id="r")
    store.put_chapter("main", "Main", "seed chapter", source="seed")
    with connect_ro(store.path) as con:
        assert con.execute("SELECT intuition_md, source FROM intuitions").fetchone()[:] == ("ember text", "ember")
        assert con.execute("SELECT synthesis_md FROM chapters").fetchone()[0] == "ember chapter"


def test_schema_two_migrates_an_old_database_in_place(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                      "INSERT INTO meta VALUES ('schema_version', '1');"
                      "CREATE TABLE triage (decl_full_name TEXT PRIMARY KEY, decl_id INTEGER, gloss TEXT NOT NULL, "
                      "work_item_id INTEGER, source TEXT NOT NULL, call_id TEXT, response_id TEXT, created_at REAL NOT NULL);"
                      "INSERT INTO triage VALUES ('A', 1, 'old', NULL, 'mock', NULL, NULL, 0);")
    con.close()
    store = ArtifactStore(path)
    store.put_triage("B", "new", source="mock", role="bridge", visualizability=2)
    with connect_ro(path) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
        assert c.execute("SELECT COUNT(*) FROM triage").fetchone()[0] == 2
        assert {r[1] for r in c.execute("PRAGMA table_info(triage)")} >= {"role", "visualizability", "visual_hint"}
    ArtifactStore(path)  # idempotent


# --- the key on a pipe -------------------------------------------------------------------------------


def test_the_key_is_read_from_a_pipe_once_and_the_fd_closed(tmp_path):
    r, w = os.pipe()
    os.write(w, b"  pipe-key-not-real-0123456789  \n")
    os.close(w)
    env = {"EDDIE_KEY_FD": str(r), "META_API_KEY": "also-set"}
    assert take_key_fd(env) is True
    assert "EDDIE_KEY_FD" not in env
    with pytest.raises(OSError):
        os.fstat(r)  # closed: nothing lingers at /proc/<pid>/fd/<n>
    keys = KeyProvider(tmp_path / "absent", env=env)
    assert keys.source == "fd" and keys.get() == "pipe-key-not-real-0123456789"
    assert "META_API_KEY" not in env
    assert "pipe-key" not in repr(keys)


def test_a_key_fd_that_is_not_a_pipe_is_refused(tmp_path):
    f = tmp_path / "file"
    f.write_text("not-a-pipe")
    fd = os.open(f, os.O_RDONLY)
    with pytest.raises(LiveEmberDisabledError):
        take_key_fd({"EDDIE_KEY_FD": str(fd)})
    with pytest.raises(LiveEmberDisabledError):
        take_key_fd({"EDDIE_KEY_FD": "not-a-number"})
    assert take_key_fd({}) is False


# --- the container sandbox's command line ------------------------------------------------------------


def test_docker_argv_mounts_only_the_job_and_has_no_network(tmp_path):
    runner = SandboxRunner(tmp_path / "art", tmp_root=tmp_path / "jobs", isolation="docker", image="eddie-sandbox:test")
    job = tmp_path / "jobs" / "eddie-sbx-x"
    argv = runner.docker_argv(job, "eddie-sbx-x")
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit 64",
                 "--rm", "--ipc none", f"--user {os.getuid()}:{os.getgid()}"):
        assert flag in joined, flag
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert mounts == [f"type=bind,source={job},target=/job",
                      f"type=bind,source={SANDBOX_SRC},target=/app/eddie/sandbox,readonly"]
    assert "-v" not in argv and str(os.path.expanduser("~")) + ":" not in joined
    assert not any("KEY" in a for a in argv)
    assert argv[argv.index("eddie-sandbox:test") + 1 :][:3] == ["python", "-I", "-B"]
    with pytest.raises(ValueError):
        SandboxRunner(tmp_path / "art", tmp_root=tmp_path / "jobs", isolation="chroot")


# --- the gateway: the journal comes before settlement, and a cancelled call is settled -------------------


def _payload(request: httpx.Request) -> dict:
    body = json.loads(request.content)
    return {"id": "resp_test_1", "object": "response", "model": body["model"], "status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                      "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}}


def test_the_journal_runs_before_the_call_is_settled():
    gateway, _, _ = make_gateway(lambda req: httpx.Response(200, json=_payload(req)))
    seen = {}

    def journal(call_id, payload):
        seen["state"] = gateway.governor.ledger.get_call(call_id)["state"]
        seen["id"] = payload["id"]

    async def go():
        async with gateway:
            return await gateway.respond({"brain_region": "R1.triage", "effort": "low", "max_output_tokens": 64,
                                          "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]},
                                         journal=journal)

    resp = asyncio.run(go())
    assert seen == {"state": "dispatched", "id": "resp_test_1"} and resp.response_id == "resp_test_1"
    assert gateway.governor.ledger.get_call(resp.call_id)["state"] == "settled"


def test_a_call_cancelled_after_sending_is_charged_not_left_dispatched():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200, json=_payload(request))

    gateway, _, _ = make_gateway(slow)

    async def go():
        task = asyncio.create_task(gateway.respond({
            "brain_region": "R1.triage", "effort": "low", "max_output_tokens": 64,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}))
        for _ in range(200):
            await asyncio.sleep(0.01)
            rows = gateway.governor.ledger.recent_calls(1)
            if rows and rows[0]["sent_at"] is not None:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await gateway.aclose()
        return gateway.governor.ledger.recent_calls(1)[0]

    row = asyncio.run(go())
    assert row["state"] == "orphan_charged" and row["actual_nano"] == row["reserved_nano"]
