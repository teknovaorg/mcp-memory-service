# Shared Project Memory

A fork of [doobidoo/mcp-memory-service](https://github.com/doobidoo/mcp-memory-service), pinned to **v12.10.0**. Apache-2.0. The upstream service and license are retained; the project-specific extension is in `handoff/` and the portable client in `client/`.

## What is stored

SQLite contains separate project, section, session, action, checkpoint, decision, incident, verification and knowledge tables. Sections such as `web`, `database` and `operations` are database entities with many-to-many record links, not folders or a tag-only prompt convention. One action can belong to several sections. Explicit sections are combined with conservative technology markers; unknown material stays `unclassified`. Customer-specific names, patterns and aliases belong in a private JSON policy selected by `MCP_HANDOFF_SECTIONS_FILE`, outside the repository. The hub installer preserves an existing private `sections.json` in its settings directory.

Search order is **section -> keywords -> semantic task context**. Each stage runs only if the previous stage found nothing. Responses expose the stage and trace. Retrieval stays inside the selected project. Historical knowledge requires `include_archive: true`. Imported content does not become verified configuration or authorization.

Native session sources are read incrementally. Converted Claude transcripts retained by Codex keep their Claude provenance; missing original action data is explicitly marked. Only visible messages, commands, tool-result excerpts, changed file references and explicit states are exported. Private reasoning, system instructions, images, credentials and full raw transcripts are not copied. Large outputs are bounded, with omissions and source locators recorded. Session history is not a working-tree sync. Continue a task through `session_context` and save explicit checkpoints, decisions, incidents and actual checks.

## Components

- Hub: one Linux process, Python 3.12, SQLite/sqlite-vec, local multilingual MiniLM L12 ONNX UINT8. The hub refuses hash embeddings. No Ollama, Redis, paid model API or PyTorch dependency.
- Client: Python 3.11+, standard library only, a versioned zipapp. macOS, Linux and Windows source paths; `CODEX_HOME` and `CLAUDE_CONFIG_DIR` overrides. Longest matching root selects the project. Use the same project ID with explicit root aliases on another host.
- MCP: local stdio bridge to `/handoff/mcp` over HTTPS. Independent credentials per project, enforced by the hub. Every stdio tool call requires an explicit project; process cwd is only a suggestion for the agent. Only bounded structured tools are exposed. The upstream admin API must remain on loopback.
- Sync: acknowledged cursors, idempotent event IDs, SQLite delta revisions, source locks, optimistic record revisions and version history. The background exporter does not require a model login or hook trust.

## Install and update

Download `bootstrap.py` from this fork's GitHub release. It validates release artifact SHA-256 checksums and installs an immutable version, preserving private config. The checksum manifest is trusted from the same GitHub repository; it is not an independent signature.

```sh
python3 bootstrap.py hub --release v12.10.0-teknova.7
python3 tools/handoff/manage.py provision --inventory private-inventory.json --url https://memory.example.org/handoff
python3 bootstrap.py client --registry /private/path/connections.json
python3 /private/path/shared-memory.py --registry /private/path/connections.json install
python3 /private/path/shared-memory.py update
```

The hub installer binds to `127.0.0.1:18765`; configure your existing HTTPS gateway to proxy **only `/handoff/*`**, with valid TLS. Provision credentials locally, distribute only the needed project keys, and never commit the private registry. The client installer merges both agents' MCP settings and hooks, preserves other tools, makes config backups, and starts a native login/background service. Codex hook trust is never altered: review the added hooks in `/hooks` or Settings. Restart agent sessions to load new MCP tools. Claude auto-memory is disabled after connecting shared memory; old files are retained and imported as references.

`shared-memory.py discover` inventories source metadata without reading authentication data. Review project ownership and archive scope before provisioning. A private registry has this shape:

```json
{"schema":1,"device_id":"unique-host-id","url":"https://memory.example.org/handoff","projects":{"example":{"token":"private-project-key","roots":["/work/example","C:/work/example"],"knowledge_scope":"active","default_sections":[]}}}
```

Run `shared-memory.py --registry ... sync`, `status`, `context --project example` or `call --project example section_list --input request.json`. `mcp --agent codex` and `mcp --agent claude` serve stdio MCP. Automatic export retains native source files; it cannot replace the assistants' internal history storage.

## One-time imports and recovery

`import-docs --project example --scope active /work/example/docs` imports Markdown and Word text as dated `imported` references. DOCX needs Pandoc; legacy DOC uses macOS textutil. Images, diagrams, executable code, generated CLI help and other assets remain at source. Memory gets a small file reference with relative path, size, type and SHA-256 instead of copying their bodies. Documentation is not watched continuously. Choose archive scope for past-employer/unverified historical runbooks. Claude Markdown memory is an incrementally synced source.

`import-legacy --project historical-example /path/old.db` reads the public agent-memory-mcp SQLite schema without changing it, preserving original dates in archive-only records. Do not merge another employer's history into active knowledge.

Every hub update takes a SQLite online backup before switching the executable. Failed startup restores the previous executable and service configuration. `tools/handoff/manage.py verify-backup /path/to/backup.sqlite3` restores into a temporary DB and checks integrity and table counts. Backups contain private data and remain outside GitHub. A future schema change requires a migration and tested rollback; the current initial schema is 1.

`session_history` accepts optional `since` (inclusive) and `until` (exclusive) ISO 8601 timestamps with explicit timezones. Bounds use event occurrence time, retain project isolation and work with the existing `after` pagination cursor. Convert a local reporting day to timezone-aware bounds before requesting it. Missing or invalid event timestamps are excluded when a time bound is supplied; unknown time is not inferred from import time. Omitting both bounds preserves the existing history interface. A new hub release installs this capability directly from the wheel; no host-specific patch is needed.

## Verification and limitations

Run `pytest -q tests/unit/test_structured_handoff.py tests/unit/test_handoff_client.py tests/unit/test_section_policy.py`. These tests cover ordering, sections, project isolation, archives, concurrent revisions, stale vectors, incremental source updates, output bounds, native paths, secret redaction and preserving hook trust. The CI matrix exercises native clients on Windows, macOS and Linux. Real-model retrieval, actual source export, service restart and backup checks must additionally be run on the target hub; unit tests intentionally do not download models.

Administrator-owned `redact_patterns` may be provisioned per project in the private inventory; they are enforced by the hub before persistence. Redaction recognizes common credential formats and private addresses. It is not a proof that arbitrary free text contains no sensitive material. Review the corpus and keep the service private. Historical sessions do not reliably encode task boundaries, final decisions or every attachment: coverage remains explicit and partial. Real model-to-model handoff additionally needs both agent logins; a protocol test alone does not establish that.

See [upstream documentation](README.upstream.md) for the original service. Fork releases do not publish to PyPI. `tools/handoff/verify_export.py --registry ... --ssh-host ...` compares acknowledged public-event IDs against a read-only hub inventory. `verify_live.py` checks independent MCP clients and real multilingual long-tail retrieval on a separate test project.
