"""Unit tests for N8NWorkflowClient — HTTP mocked at the session level."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from logpose.workflows.n8n_client import (
    N8NWorkflowClient,
    WorkflowBadResponseError,
    WorkflowInvocationError,
)

_URL = "https://n8n.example.com/webhook/test"


def _response(status_code: int, json_body: Any = None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


def _client(session: MagicMock, **kwargs: Any) -> N8NWorkflowClient:
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("backoff_seconds", 0)  # no sleeping in tests
    return N8NWorkflowClient(_URL, session=session, **kwargs)


def test_invoke_returns_json_object_on_success() -> None:
    session = MagicMock()
    session.post.return_value = _response(200, {"extracted": {"user": "alice"}})

    result = _client(session).invoke('{"id": "a1"}')

    assert result == {"extracted": {"user": "alice"}}
    session.post.assert_called_once()
    assert session.post.call_args.args[0] == _URL


def test_invoke_sends_auth_header_when_configured() -> None:
    session = MagicMock()
    session.post.return_value = _response(200, {})

    _client(
        session,
        auth_header_name="X-N8N-Auth",
        auth_header_value="secret",
    ).invoke("{}")

    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-N8N-Auth"] == "secret"


def test_invoke_retries_on_connection_error_then_succeeds() -> None:
    session = MagicMock()
    session.post.side_effect = [
        requests.ConnectionError("refused"),
        _response(200, {"ok": True}),
    ]

    result = _client(session).invoke("{}")

    assert result == {"ok": True}
    assert session.post.call_count == 2


def test_invoke_retries_on_5xx_then_raises_retryable() -> None:
    session = MagicMock()
    session.post.return_value = _response(502, text="bad gateway")

    with pytest.raises(WorkflowInvocationError) as exc_info:
        _client(session).invoke("{}")

    assert exc_info.value.retryable is True
    assert session.post.call_count == 3  # max_attempts


def test_invoke_does_not_retry_on_4xx() -> None:
    session = MagicMock()
    session.post.return_value = _response(404, text="webhook not registered")

    with pytest.raises(WorkflowInvocationError) as exc_info:
        _client(session).invoke("{}")

    assert exc_info.value.retryable is False
    session.post.assert_called_once()


def test_invoke_raises_bad_response_on_non_json_body() -> None:
    session = MagicMock()
    session.post.return_value = _response(200, json_body=None, text="<html>")

    with pytest.raises(WorkflowBadResponseError):
        _client(session).invoke("{}")


def test_invoke_raises_bad_response_on_json_array() -> None:
    session = MagicMock()
    session.post.return_value = _response(200, json_body=[1, 2, 3])

    with pytest.raises(WorkflowBadResponseError):
        _client(session).invoke("{}")


def test_rejects_zero_max_attempts() -> None:
    with pytest.raises(ValueError):
        N8NWorkflowClient(_URL, max_attempts=0)
