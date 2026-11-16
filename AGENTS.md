# Fork workflow

This fork targets upstream v12.10.0 plus the structured handoff extension. Preserve licenses and distinguish upstream versions from fork releases. Do not publish private connection profiles, authentication files, databases or real conversation content. Use synthetic fixtures in tests.

Use source search for the affected component. GitNexus is optional when its server and skill files are actually available; a missing integration must not block repository work. Client source readers remain read-only. Never modify Codex hook trust, native history, or user credentials. Keep project/employer boundaries enforced server-side.

Relevant checks: `pytest -q tests/unit/test_structured_handoff.py tests/unit/test_handoff_client.py tests/unit/test_section_policy.py`, portable zipapp protocol checks, real ONNX hub checks, incremental export replay, persistence and backup restore checks. Do not claim physical Windows or Claude model testing based on synthetic clients alone. Never publish upstream issue comments or PyPI releases as part of this fork's workflow.

Customer names, account-specific paths and technology rules belong in private configuration outside the checkout. Examples and fixtures must be synthetic. Before publishing, run `tools/handoff/check_public.py` with the operator-owned private policy and inspect every release artifact. Never put the policy itself in Git.
