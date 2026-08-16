# lup: ignore[cast, dict-get, own-model-dispatch]
# Test fixtures and assertions construct these shapes deliberately.
# That the direct-call path hands back the declared output model rather than
# the serialized MCP response is the assertion itself, observed from outside
# the decorator, not a branch taken on the way to one.
"""Behavior tests for the lup_tool decorator and LupMcpTool.

Covers the three response paths of the SDK-facing handler (success,
ToolError, input validation failure), the direct-call path that bypasses
MCP serialization, and the JSON mode the serialized path renders in.
"""

import json
from datetime import datetime, timezone
from typing import cast

from pydantic import BaseModel, Field

from lup.mcp import ToolError, ToolResponse, lup_tool


class EchoInput(BaseModel):
    text: str = Field(description="Text to echo")


class EchoOutput(BaseModel):
    text: str


@lup_tool("Echo the input text back.", name="echo")
async def echo(inp: EchoInput) -> EchoOutput:
    if inp.text == "boom":
        raise ToolError("exploded")
    return EchoOutput(text=inp.text)


class StampedOutput(BaseModel):
    at: datetime


@lup_tool("Return a fixed timestamp.", name="stamped")
async def stamped(inp: EchoInput) -> StampedOutput:
    _ = inp
    return StampedOutput(
        at=datetime(2026, 8, 16, 19, 40, 11, 123456, tzinfo=timezone.utc)
    )


def response_text(resp: ToolResponse) -> str:
    content = resp.get("content")
    assert content is not None
    block = content[0]
    assert block["type"] == "text"
    return block["text"]


async def test_direct_call_returns_typed_output() -> None:
    result = await echo(EchoInput(text="hi"))
    assert isinstance(result, EchoOutput)
    assert result.text == "hi"


async def test_sdk_handler_serializes_output_as_json() -> None:
    resp = cast(ToolResponse, await echo.handler({"text": "hi"}))
    assert not resp.get("is_error")
    assert json.loads(response_text(resp)) == {"text": "hi"}


async def test_tool_error_becomes_is_error_response() -> None:
    resp = cast(ToolResponse, await echo.handler({"text": "boom"}))
    assert resp.get("is_error") is True
    assert "exploded" in response_text(resp)


async def test_invalid_input_becomes_is_error_with_message() -> None:
    resp = cast(ToolResponse, await echo.handler({"wrong_field": 1}))
    assert resp.get("is_error") is True
    assert "Invalid input" in response_text(resp)


async def test_datetime_output_renders_as_iso_8601() -> None:
    """A result crosses a JSON wire, so pydantic's JSON mode renders it.

    Python mode hands back a live ``datetime`` and leaves ``default=str`` to
    spell it, which puts a space where ISO 8601 wants a ``T`` — a difference
    an agent reads, because the rendered string is the value it reasons over.
    """
    resp = cast(ToolResponse, await stamped.handler({"text": "x"}))
    at = json.loads(response_text(resp))["at"]
    assert at == "2026-08-16T19:40:11.123456Z"


def test_models_inferred_from_annotations() -> None:
    assert echo.input_model is EchoInput
    assert echo.output_model is EchoOutput
    assert echo.name == "echo"
    assert echo.input_schema == EchoInput.model_json_schema()
