"""Bounded input contracts. Imported observations never imply verified decisions."""

from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

Short = Annotated[str, Field(max_length=512)]
Text = Annotated[str, Field(max_length=8192)]
Identifier = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[\w.\-:@/]+$")
]
Project = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
]
Items = Annotated[list[Short], Field(max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Project
    topics: Annotated[
        list[Annotated[str, Field(max_length=80, pattern=r"^[a-z0-9][a-z0-9+_.-]*$")]],
        Field(max_length=20),
    ] = Field(
        default_factory=list,
        validation_alias=AliasChoices("sections", "topics"),
        description="Technology sections, e.g. web, database, operations. Independent of project/session; a record may have several.",
    )


class Provenance(Input):
    agent: Short = "unknown"
    session_id: Short = ""
    task_id: Short = ""
    source: Short = "explicit-agent-record"
    observed_at: Short = ""


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: Identifier
    source_revision: Annotated[int, Field(ge=0)] = 0
    topics: Annotated[list[Short], Field(max_length=20)] = Field(
        default_factory=list, validation_alias=AliasChoices("sections", "topics")
    )
    kind: Literal[
        "user_message",
        "assistant_message",
        "command",
        "file_change",
        "mcp_call",
        "web_search",
        "compaction",
        "coverage",
        "artifact",
    ]
    occurred_at: Short = ""
    summary: Text = ""
    command: Annotated[str, Field(max_length=4096)] = ""
    outcome: Literal[
        "unknown", "running", "completed", "failed", "interrupted", "skipped"
    ] = "unknown"
    exit_code: int | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    files: Items = Field(default_factory=list)
    excerpt: Annotated[str, Field(max_length=4096)] = ""
    omitted_bytes: Annotated[int, Field(ge=0)] = 0
    source_ref: Short = ""


class Ingest(Input):
    session_id: Identifier
    agent: Short
    source: Short
    title: Short = ""
    task_id: Short = ""
    cursor: Short = ""
    coverage: Literal["partial", "observed"] = "partial"
    gaps: Items = Field(default_factory=list)
    events: Annotated[list[Event], Field(max_length=100)]


class Checkpoint(Provenance):
    task_id: Identifier
    title: Short
    objective: Text
    state: Text
    completed: Items = Field(default_factory=list)
    next_steps: Items = Field(default_factory=list)
    open_questions: Items = Field(default_factory=list)
    changed_files: Items = Field(default_factory=list)
    verification_ids: Items = Field(default_factory=list)
    limitations: Items = Field(default_factory=list)
    branch: Short = ""
    commit: Short = ""
    status: Literal["active", "waiting", "completed"] = "active"
    base_revision: Annotated[int, Field(ge=0)] = 0


class Decision(Provenance):
    record_id: Identifier
    title: Short
    choice: Text
    rationale: Text
    alternatives: Items = Field(default_factory=list)
    status: Literal["proposed", "accepted", "superseded"] = "proposed"
    supersedes: Short = ""
    base_revision: Annotated[int, Field(ge=0)] = 0


class Incident(Provenance):
    record_id: Identifier
    signature: Short
    problem: Text
    cause: Text = ""
    resolution: Text = ""
    verification_ids: Items = Field(default_factory=list)
    status: Literal["open", "resolved", "unverified"] = "unverified"
    base_revision: Annotated[int, Field(ge=0)] = 0


class Verification(Provenance):
    record_id: Identifier
    command: Text = ""
    scope: Text
    outcome: Literal["passed", "failed", "not_run", "inconclusive"]
    exit_code: int | None = None
    evidence: Text
    limitations: Items = Field(default_factory=list)
    base_revision: Annotated[int, Field(ge=0)] = 0


class Knowledge(Provenance):
    record_id: Identifier
    title: Short
    statement: Text
    category: Literal[
        "decision", "learning", "preference", "project_note", "document"
    ] = "project_note"
    scope: Literal["active", "archive"] = "active"
    status: Literal["verified", "unverified", "imported", "superseded"] = "unverified"
    source_revision: Short = ""
    base_revision: Annotated[int, Field(ge=0)] = 0


class Context(Input):
    task_id: Short = ""
    query: Short = ""
    limit: Annotated[int, Field(ge=1, le=20)] = 5
    max_chars: Annotated[int, Field(ge=2000, le=50000)] = 20000


class History(Input):
    session_id: Short = ""
    task_id: Short = ""
    after: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 30
    since: Short = Field(default="", description="Inclusive ISO 8601 timestamp with timezone; optional.")
    until: Short = Field(default="", description="Exclusive ISO 8601 timestamp with timezone; optional.")


class Search(Input):
    query: Short
    context: Text = ""
    include_archive: bool = False
    limit: Annotated[int, Field(ge=1, le=30)] = 8


class KnowledgeGet(Input):
    record_id: Identifier
    include_versions: bool = False


CONTRACTS = {
    "knowledge_get": (
        KnowledgeGet,
        True,
        "Read one structured fact and optionally its previous versions before editing; includes current revision and provenance.",
    ),
    "section_list": (
        Input,
        True,
        "List this project's technology sections and record counts, such as Web, Database and Operations. The same technology does not merge different projects.",
    ),
    "session_ingest": (
        Ingest,
        False,
        "Import bounded visible actions. Never submit reasoning, system instructions, raw transcripts or credentials.",
    ),
    "task_checkpoint": (
        Checkpoint,
        False,
        "Save explicit task state for another assistant. Read session_context first and pass base_revision to prevent overwriting newer work.",
    ),
    "decision_record": (
        Decision,
        False,
        "Record a decision with its stated rationale, alternatives, status and provenance. Proposed is the default; do not infer user approval.",
    ),
    "incident_record": (
        Incident,
        False,
        "Record an error, cause, resolution and verification references separately. Missing evidence stays unverified.",
    ),
    "verification_record": (
        Verification,
        False,
        "Record what was actually tested, evidence, result and limitations. A successful start alone is not an end-to-end check.",
    ),
    "knowledge_record": (
        Knowledge,
        False,
        "Save a bounded project fact or imported note with source, status and active/archive scope. Read before updating and pass base_revision.",
    ),
    "session_context": (
        Context,
        True,
        "START HERE when continuing work: project-scoped active task checkpoints, explicit decisions, open errors and recent visible actions. Does not restore private model state.",
    ),
    "session_history": (
        History,
        True,
        "Read structured visible actions incrementally by session/task. after is an opaque sequence watermark; output excerpts are intentionally bounded.",
    ),
    "knowledge_search": (
        Search,
        True,
        "Hybrid semantic/keyword search of this project's structured knowledge. Archive is excluded unless include_archive=true.",
    ),
}
