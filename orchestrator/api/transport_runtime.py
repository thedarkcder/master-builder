from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse, Response

from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportAction,
)


class HttpTransportExecutor:
    def execute(self, *, action: TransportAction) -> Response:
        if isinstance(action, HttpJsonResponseAction):
            return JSONResponse(
                status_code=action.status_code,
                content=dict(action.content),
                headers=dict(action.headers),
            )
        if isinstance(action, HttpJsonResponseBytesAction):
            body = action.body.encode("utf-8") if isinstance(action.body, str) else action.body
            return Response(
                status_code=action.status_code,
                content=body,
                media_type="application/json",
                headers=dict(action.headers),
            )
        raise RuntimeError(f"Unsupported HTTP transport action: {type(action).__name__}")


def http_json_response_action(*, status_code: int, content: dict) -> HttpJsonResponseAction:
    return HttpJsonResponseAction(status_code=status_code, content=content)


def json_response_to_action(response: JSONResponse) -> HttpJsonResponseBytesAction:
    return HttpJsonResponseBytesAction(
        status_code=response.status_code,
        body=response.body,
        headers=dict(response.headers),
    )


def execute_http_ingress_result(
    *,
    result: IngressResult,
    task_scheduler=asyncio.create_task,
) -> Response:
    response_action: TransportAction | None = None
    for deferred in result.deferred_work:
        task = task_scheduler(deferred.runner())
        if deferred.on_scheduled is not None:
            deferred.on_scheduled(task)
    for action in result.actions:
        if isinstance(action, (HttpJsonResponseAction, HttpJsonResponseBytesAction)):
            response_action = action
            break
    if response_action is None:
        raise RuntimeError("IngressResult for HTTP transport did not include an HTTP response action")
    return HttpTransportExecutor().execute(action=response_action)


def decode_json_body(action: TransportAction) -> dict:
    if isinstance(action, HttpJsonResponseAction):
        return dict(action.content)
    if not isinstance(action, HttpJsonResponseBytesAction):
        raise TypeError(f"Transport action body was not an HTTP JSON action: {type(action).__name__}")
    body = action.body
    if isinstance(body, bytes):
        return json.loads(body.decode("utf-8"))
    if isinstance(body, str):
        return json.loads(body)
    raise TypeError("Transport action body was not JSON bytes or string")
