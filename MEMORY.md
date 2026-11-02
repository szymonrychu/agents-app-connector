# MEMORY

Dated entries, newest last. Supersede an entry rather than deleting it. One line per non-obvious decision.

2026-11-02: decisions D1-D6 of the boox-sketch-translator plan (workbench `docs/plans/boox-sketch-translator/plan.md`, section 9): D1 images travel as message attachments through orchestrator and wrapper; D2 spec goes to the app plus hand-off to a chosen session; D3 one session per conversation, follow-ups refine; D4 public path with a bearer device token; D5 model claude-sonnet-5-5 in the preset, 1568 px long edge, all limits configurable; D6 one service with the translator as the `sketch` plugin.
2026-11-02: AC_VERSION = v0.8.0 (agents-common with attachments, download_file, runs pass-through and list_sessions); Task 19 of the plan releases the next one.
2026-11-02: this repo is treated as public (user ruling): no secrets, internal hostnames, cluster names or personal data in files or history; deployment values live only in agents-helmfile.
2026-11-02: schema uses an `events` table (cursor-polled by conversation, no delivery status) instead of the spec 3.1 `outbound` queue (nothing consumes a delivery status), `devices.token_sha256`, and `bindings` instead of `conversations.session_id`; the spec section 3.1 is amended in the workbench (preflight C6). `events.type` additionally allows `handoff`.
2026-11-02: uuid FKs from runs/bindings/holds/events/handoffs/specs to conversations cascade on delete; inbound.run_id, handoffs.run_id and bindings.run_id carry no FK so the worker can write rows in either order.
