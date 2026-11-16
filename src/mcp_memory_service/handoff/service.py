"""One shared model, typed records and explicitly partitioned retrieval."""

import asyncio
import json
import hashlib

from .models import CONTRACTS
from .redact import clean, project_clean
from .store import HandoffStore, RECORDS
from .topics import classify, normalize


class HandoffService:
    def __init__(self, storage, memory_service):
        self.storage = storage
        self.memory_service = memory_service
        path = getattr(storage, "db_path", None)
        if not path:
            raise ValueError("Structured handoff currently requires SQLite-vec storage")
        self.records = HandoffStore(path)
        self._knowledge_lock = asyncio.Lock()

    async def call(self, name, arguments):
        if name not in CONTRACTS:
            raise ValueError("Unknown handoff tool")
        contract = CONTRACTS[name][0]
        arguments = dict(arguments)
        for field in ("topics", "sections"):
            if field in arguments:
                arguments[field] = [normalize(x) for x in arguments[field]]
        data = clean(contract.model_validate(arguments).model_dump())
        data = project_clean(data, data["project"])
        if name == "section_list":
            return self.records.topic_list(data["project"])
        if name == "knowledge_get":
            data.pop("topics", None)
            return self.records.knowledge_get(**data)
        if name in RECORDS:
            # All durable record types participate in the same staged search.
            async with self._knowledge_lock:
                result = self.records.record(name, dict(data))
                await self._index(result["record"], result, name)
                return result
        if name == "session_ingest":
            return self.records.ingest(data)
        if name == "session_context":
            query = data.pop("query")
            budget = data.pop("max_chars")
            result = self.records.context(**data)
            if query:
                result["knowledge"] = await self.search(
                    data["project"], query, False, data["limit"], data["topics"]
                )
            return bounded_context(result, budget)
        if name == "session_history":
            return self.records.history(**data)
        if name == "knowledge_search":
            return await self.staged_search(**data)
        raise ValueError("Unsupported handoff operation")

    async def _index(self, record, result, tool="knowledge_record"):
        entity_type, _, key = RECORDS[tool]
        project, record_id = record["project"], record[key]
        scope = record["scope"] if tool == "knowledge_record" else "active"
        previous = self.records.vector(project, record_id, entity_type)
        if (
            not result["changed"]
            and previous
            and all(p["record_revision"] == record["revision"] for p in previous)
        ):
            result["indexed"] = True
            return
        title, body = self.records.document(record)
        prefix = f"[{hashlib.sha256(project.encode()).hexdigest()[:16]}|{entity_type}|{hashlib.sha256(record_id.encode()).hexdigest()[:16]}|{scope}]\n{title[:160]}\n"
        metadata = {
            "project": project,
            "record_id": record_id,
            "scope": scope,
            "source": record["source"],
            "status": record.get("status", record.get("outcome", "unknown")),
            "structured_handoff": True,
        }
        try:
            tags = [f"project:{project}", "structured-handoff"] + [
                f"topic:{topic}" for topic in record["topics"]
            ]
            hashes = []
            for index, body_chunk in enumerate(self._chunks(body, prefix)):
                content = prefix + f"[part {index}]\n" + body_chunk
                # Immutable vector fragments. Typed revisions determine which
                # fragments are current; a retry can reuse an exact fragment.
                saved = await self.memory_service.store_memory(
                    content,
                    tags=tags,
                    memory_type="reference",
                    metadata=dict(metadata, chunk_index=index),
                    conversation_id=f"handoff:{project}:{entity_type}:{record_id}",
                    store=f"hf:{project}:{scope}",
                )
                memory = saved.get("memory", {})
                content_hash = memory.get("content_hash") or saved.get("content_hash")
                if not saved.get("success"):
                    from ..utils.hashing import generate_content_hash

                    expected = generate_content_hash(content)
                    existing = await self.storage.get_by_hash(
                        expected, store=f"hf:{project}:{scope}"
                    )
                    if existing and existing.content == content:
                        content_hash = expected
                    else:
                        raise RuntimeError("Fragment indexing failed")
                if not content_hash:
                    raise RuntimeError("Index response omitted hash")
                hashes.append(content_hash)
            self.records.set_vectors(
                project, record_id, hashes, scope, entity_type, record["revision"]
            )
            result["indexed"] = True
            result["index_fragments"] = len(hashes)
        except Exception:
            # Do not lose a verified record because a model/provider is down,
            # and do not echo exception strings that can carry input content.
            result["indexed"] = False
            result["index_warning"] = (
                "Structured record saved; semantic indexing unavailable"
            )

    def _chunks(self, body, prefix):
        tokenizer = getattr(
            getattr(self.storage, "embedding_model", None), "_tokenizer", None
        )
        if tokenizer is None:
            return [body[i : i + 1000] for i in range(0, max(1, len(body)), 1000)]
        encoded = tokenizer.encode(body, add_special_tokens=False)
        ids = list(encoded.ids)
        for overflow in encoded.overflowing:
            ids.extend(overflow.ids)
        budget = max(32, 320 - len(tokenizer.encode(prefix).ids))
        return [
            tokenizer.decode(ids[i : i + budget], skip_special_tokens=True)
            for i in range(0, max(1, len(ids)), max(1, budget - 32))
        ]

    async def search(self, project, query, include_archive=False, limit=8, topics=()):
        result = await self.staged_search(
            project, query, include_archive, limit, topics
        )
        return result["records"]

    async def staged_search(
        self, project, query, include_archive=False, limit=8, topics=(), context=""
    ):
        sections = list(topics) or classify(query)
        trace = []
        if sections:
            records = self.records.section_search(
                project, query, sections, include_archive, limit
            )
            trace.append(
                {"stage": "section", "sections": sections, "found": len(records)}
            )
            if records:
                return {
                    "project": project,
                    "stage": "section",
                    "records": records,
                    "trace": trace,
                    "archive_included": include_archive,
                }
        else:
            trace.append(
                {
                    "stage": "section",
                    "sections": [],
                    "found": 0,
                    "reason": "No section supplied or confidently inferred",
                }
            )
        keyword = self.records.keyword_search(project, query, include_archive, limit)
        trace.append({"stage": "keywords", "found": len(keyword)})
        if keyword:
            return {
                "project": project,
                "stage": "keywords",
                "records": keyword,
                "trace": trace,
                "archive_included": include_archive,
            }
        hashes = []
        try:
            for scope in ["active", "archive"] if include_archive else ["active"]:
                semantic_query = query + (
                    "\nTask context: " + context[:1000] if context else ""
                )
                results = await self.storage.retrieve(
                    semantic_query,
                    n_results=min(100, limit * 8),
                    store=f"hf:{project}:{scope}",
                )
                hashes.extend(
                    x.memory.content_hash for x in results if x.relevance_score >= 0.3
                )
        except Exception:
            pass
        semantic = self.records.knowledge_by_hashes(project, hashes, include_archive)
        seen = set()
        out = []
        # Exact version strings should remain visible beside semantic matches.
        for row in semantic:
            identity = (
                row.get("record_type"),
                row.get("record_id", row.get("task_id")),
            )
            if identity not in seen:
                seen.add(identity)
                out.append(row)
        trace.append({"stage": "context", "found": len(out[:limit])})
        return {
            "project": project,
            "stage": "context" if out else "none",
            "records": out[:limit],
            "trace": trace,
            "archive_included": include_archive,
        }


def tools():
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": contract.model_json_schema(),
            "annotations": {"readOnlyHint": read_only, "destructiveHint": False},
        }
        for name, (contract, read_only, description) in CONTRACTS.items()
    ]


def wire(value):
    if isinstance(value, dict):
        return {"sections" if k == "topics" else k: wire(v) for k, v in value.items()}
    if isinstance(value, list):
        return [wire(x) for x in value]
    return value


def bounded_context(result, budget):
    if len(json.dumps(result, ensure_ascii=False)) <= budget:
        return result
    result["context_truncated"] = True

    # Keep valid structured JSON and locators for requesting details.
    def shorten(value):
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + " [excerpt; request record details]"
        if isinstance(value, list):
            return [shorten(x) for x in value[:20]]
        if isinstance(value, dict):
            return {k: shorten(v) for k, v in value.items()}
        return value

    result = shorten(result)
    for key in (
        "recent_actions",
        "sessions",
        "verifications",
        "knowledge",
        "decisions",
        "incidents",
        "tasks",
    ):
        while (
            len(json.dumps(result, ensure_ascii=False)) > budget
            and len(result.get(key, [])) > 1
        ):
            result[key].pop()
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        result = {
            "project": result["project"],
            "context_truncated": True,
            "tasks": [
                {k: r[k] for k in ("task_id", "title", "status", "revision") if k in r}
                for r in result.get("tasks", [])
            ],
            "notice": "Context exceeded the requested budget. Choose a task and request its details with a larger max_chars budget.",
        }
    return result
