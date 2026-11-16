"""Shared handoff dispatch for every existing MCP transport."""

import json

from mcp import types
from ...handoff.service import HandoffService, wire


async def service(server):
    await server._ensure_storage_initialized()
    if not hasattr(server, "handoff_service"):
        server.handoff_service = HandoffService(server.storage, server.memory_service)
    return server.handoff_service


async def dispatch(server, name, arguments):
    result = await (await service(server)).call(name, arguments)
    return [
        types.TextContent(
            type="text", text=json.dumps(wire(result), ensure_ascii=False, default=str)
        )
    ]


def handler(name):
    async def call(server, arguments):
        return await dispatch(server, name, arguments)

    return call


session_ingest = handler("session_ingest")
task_checkpoint = handler("task_checkpoint")
decision_record = handler("decision_record")
incident_record = handler("incident_record")
verification_record = handler("verification_record")
knowledge_record = handler("knowledge_record")
session_context = handler("session_context")
session_history = handler("session_history")
knowledge_search = handler("knowledge_search")
section_list = handler("section_list")
knowledge_get = handler("knowledge_get")
