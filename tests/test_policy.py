import pytest

from eddie.llm.body import build_body
from eddie.llm.errors import ForbiddenParameterError, MissingParameterError
from eddie.llm.policy import REGION_POLICY, check_request, validate_wire_body
from eddie.llm.types import EddieRequest


def _req(**kw):
    base = {"brain_region": "R1.triage", "effort": "minimal", "max_output_tokens": 64}
    base.update(kw)
    return EddieRequest.simple("hi", **base)


def test_region_effort_allowlists_match_the_plan():
    assert REGION_POLICY["R1.triage"].efforts == {"minimal", "low"}
    assert REGION_POLICY["R1.worker"].efforts == {"low", "medium", "high"}
    assert REGION_POLICY["R2.critic"].efforts == {"low", "medium", "high"}
    assert REGION_POLICY["R3.arbiter"].efforts == {"high", "xhigh", "max"}
    assert REGION_POLICY["R4.consolidator"].efforts == {"medium", "high"}


@pytest.mark.parametrize(
    "region,effort",
    [("R1.triage", "high"), ("R1.triage", "max"), ("R2.critic", "max"), ("R4.consolidator", "minimal")],
)
def test_region_effort_mismatch_is_refused(region, effort):
    with pytest.raises(ForbiddenParameterError):
        check_request(_req(brain_region=region, effort=effort))


def test_ceilings_and_minimum():
    with pytest.raises(ForbiddenParameterError):
        check_request(_req(max_output_tokens=2_049))
    with pytest.raises(ForbiddenParameterError):
        check_request(_req(max_output_tokens=15))
    check_request(_req(max_output_tokens=16))


def test_arbiter_requires_concise_summary():
    with pytest.raises(ForbiddenParameterError):
        check_request(_req(brain_region="R3.arbiter", effort="xhigh", max_output_tokens=1_000))
    check_request(
        _req(brain_region="R3.arbiter", effort="xhigh", max_output_tokens=1_000, reasoning_summary="concise")
    )


def test_missing_mandatory_parameters():
    with pytest.raises(MissingParameterError):
        EddieRequest.simple("hi", brain_region="R1.triage", max_output_tokens=64)  # no effort
    with pytest.raises(MissingParameterError):
        EddieRequest.simple("hi", brain_region="R1.triage", effort="minimal")  # no max_output_tokens
    with pytest.raises(MissingParameterError):
        EddieRequest.simple("hi", effort="minimal", max_output_tokens=64)  # no region


def test_effort_none_and_unknown_fields_are_forbidden():
    with pytest.raises(ForbiddenParameterError):
        _req(effort="none")
    with pytest.raises(ForbiddenParameterError):
        _req(tools=[{"type": "web_search"}])
    with pytest.raises(ForbiddenParameterError):
        _req(previous_response_id="resp_1")


def test_canonical_body_is_explicit_and_store_false():
    body = build_body(_req())
    assert body["reasoning"] == {"effort": "minimal"}
    assert body["max_output_tokens"] == 64
    assert body["store"] is False
    validate_wire_body(body)


@pytest.mark.parametrize(
    "key,value",
    [
        ("tools", [{"type": "web_search"}]),
        ("previous_response_id", "resp_1"),
        ("background", True),
        ("stream", True),
        ("truncation", "auto"),
        ("conversation", "conv_1"),
        ("service_tier", "flex"),
        ("prompt", {"id": "p"}),
        ("prompt_cache_retention", "24h"),
    ],
)
def test_wire_schema_forbids_hidden_context_and_surcharges(key, value):
    body = build_body(_req())
    body[key] = value
    with pytest.raises(ForbiddenParameterError):
        validate_wire_body(body)


@pytest.mark.parametrize("mutate", ["drop_store", "store_true", "drop_effort", "effort_none", "drop_max"])
def test_wire_schema_mandatory_fields(mutate):
    body = build_body(_req())
    if mutate == "drop_store":
        del body["store"]
    elif mutate == "store_true":
        body["store"] = True
    elif mutate == "drop_effort":
        body["reasoning"] = {}
    elif mutate == "effort_none":
        body["reasoning"] = {"effort": "none"}
    elif mutate == "drop_max":
        del body["max_output_tokens"]
    with pytest.raises((ForbiddenParameterError, MissingParameterError)):
        validate_wire_body(body)


def test_wire_schema_allows_temperature_and_text():
    body = build_body(
        _req(temperature=0.2, text_format={"format": {"type": "json_schema", "name": "x", "schema": {}}})
    )
    validate_wire_body(body)
    body["temperature"] = 3
    with pytest.raises(ForbiddenParameterError):
        validate_wire_body(body)


def test_wire_schema_rejects_file_ids_and_reasoning_items():
    body = build_body(_req())
    body["input"][0]["content"].append({"type": "input_file", "file_id": "file-123"})
    with pytest.raises(ForbiddenParameterError):
        validate_wire_body(body)
    body = build_body(_req())
    body["input"].append({"type": "reasoning", "encrypted_content": "..."})
    with pytest.raises(ForbiddenParameterError):
        validate_wire_body(body)
