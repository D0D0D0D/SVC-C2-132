# Test Specification — SVC-C2-132 Game Live-Ops Player-Promise Impact Review Agent

## Test Strategy
- Test types: Unit (`tests/unit/`) / Integration (`tests/integration/`) / Proof-of-Boundary (`tests/proof_of_boundary/`)
- Framework provided by CI wheel; the LLM and KB/export stores are injection seams
  (`src/services/llm_seam.py`, `kb_store.py`, `export_store.py`) so tests run offline.

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict (`src/schemas/state.py`) | No Pydantic/dataclass — `test_state_safety` | PASS |
| TC-02 | SecurityViolationError fires on bad provenance / assertive output | Error raised → status=error — `test_graph_invoke::test_provenance_reject_on_bad_citation`, `test_lexicon` | PASS |
| TC-03 | No JWT/Credential in State/src | CI の資格情報スキャン: 0 violations; `test_checkpoint_safety` no-cred assertions | PASS (local) |
| TC-04 | InvocationContext via `config["configurable"]`/`from_state` only | GraphNode uses `InvocationContext.from_state` (framework) | PASS |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | node_start/complete/error not emitted in execute bodies | PASS |
| TC-06 | S-2 `_security_gate_input()` not overridable (`FunctionNode`) | TypeError at class def — `test_framework_compliance_tc06_tc07` | PASS |
| TC-07 | S-3 `_security_gate_output()` not overridable (`FunctionNode`) | TypeError at class def — same | PASS |
| TC-08 | `required_trust_level` enforced (all nodes VERIFIED_EXTERNAL) | PB-6 exercises `node(state)`; RBAC denial → error | PASS |
| TC-09 | S-2 `_extra_security_gate_input()` non-trivial | pre_process: mode/RBAC/closed-schema/payload_ref reject | PASS |
| TC-10 | S-3 `_extra_security_gate_output()` non-trivial | post_process: legal-assertion lexicon + prohibited keys + marker | PASS |
| TC-11 | S-4 ≥1 domain `emit_trace_event()` per `execute()` | `test_pb1_audit_events` measures domain emits across invoke | PASS |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Test | Result |
|-------|----------|------|--------|
| PB-1 | BaseNode → AuditLogger | domain events (input_validated/reconcile/gap_judgment/kb_recall/output_finalized) measured by patching each node module — `test_pb1_audit_events` | PASS |
| PB-2 | State serialization | primitives-only, deserialise round-trip — `test_checkpoint_safety` | PASS |
| PB-3 | L1 → external service (LLM/KB) | injection seams; FakeLLM E2E + reference KB store (`test_graph_invoke`, `test_kb_persistence_isolation`) | PASS (seam) |
| PB-4 | Import isolation | no Level 0 imports — `test_import_isolation` | PASS |
| PB-5 | Checkpoint safety | payload_ref keeps the large export OUT of State; deserialise check — `test_checkpoint_safety` | PASS |
| PB-6 | Invoke execution order | S-1 → node_start → S-2 → execute → S-3 → node_complete for every node — `test_pb_invoke_order` | PASS |
| PB-7 | HITL interrupt propagation | **Auto-waived — non-HITL** (`config/config.yaml` `hitl.enabled: false`); stub auto-skips | WAIVED |

### Accumulation-type / provenance PB (design §(a),(b) — required at implementation)

| PB-ID | Boundary | Test | Result |
|-------|----------|------|--------|
| PB-8 (layer-1) | checkpoint isolation | large `event_config` via `payload_ref` never in `validated_input`; deserialise round-trip — `test_checkpoint_safety` | PASS |
| PB-9 (layer-2) | persistent-KB governance | tenant/org RBAC isolation + retention removes raw at the persistence layer, structured-retained kept — `test_kb_persistence_isolation` | PASS |
| PB-10 (provenance) | citation binding | valid + short/non-literal quote / absent id / EXISTING-but-unrelated id / numeric mismatch (bidirectional) / version / span — `test_provenance` | PASS |
| PB-11 (index axis) | search-axis separation | recall order unchanged when verdict/rationale (conclusion vocab) swapped — `test_kb_store::test_index_axis_separation` | PASS |

## Business Logic Tests

| TC-ID | Test | Result |
|-------|------|--------|
| BL-01 | invoke E2E: FakeLLM gap findings pass provenance, projection non-assertive | PASS |
| BL-02 | RBAC fail-closed: empty scope / read-scope cannot ingest | PASS |
| BL-03 | ingest write + injection-in-field fail-closed (writes nothing) | PASS |
| BL-04 | feedback requires an existing case in scope | PASS |
| BL-05 | no-LLM degradation returns empty findings but status=success | PASS |
| BL-06 | config fail-closed: `kb_raw_retention_days` 0/neg/non-int/bool rejected | PASS |
| BL-07 | numerics: NFKC + kanji + unit-adjacent extraction (`999999円`, `六十`, `六十分`) | PASS |
| BL-08 | write-path sanitiser masks PII, rejects prompt-injection | PASS |
| BL-09 | **B-6 RBAC trust boundary — forged scope cannot authorize**: a trusted caller (INVOKE_AUTH_TOKEN) with a full-permission but UNSIGNED / wrong-signature `X-Authorized-Scopes` header is denied; only a valid gateway-signed header or in-process `request.state` injection authorizes (same caller/body — the difference is only the scope channel) — `test_server_rbac_boundary` | PASS |
| BL-10 | **B-6 fail-closed**: `_trusted_scopes` returns `[]` for a raw/unsigned header and when no `SCOPE_SIGNING_KEY` is configured (even a locally-signed header) — `test_server_rbac_boundary` | PASS |

## Live-LLM Verification (docs/02 §(b), MunicipalContractVariationEvidenceBriefAgent lesson)

The canonical live-model check (an external-CLI-backed BaseLLM) could not run in the
offline dev environment (no venv, no CLI available). Instead the **prompt-assembly defect it
targets is covered structurally**: `test_promise.py`-equivalent checks confirm
`build_promise_prompt` and `build_gap_prompt` embed the full JSON schema BODY (not just
its name) and the concrete `source_registry` (id/kind/version/span/text/facts), so the
LLM cannot receive a schema-less prompt that would emit `{}`. The FakeLLM E2E then drives
a realistic gap payload through parse → provenance → non-assertive projection and asserts
**non-empty, provenance-valid** findings — not merely "not degraded".

## Test Execution Summary
- Execution: 2026-08-05 — `pytest tests/` (framework wheel), offline seams (incl. B-6 boundary suite)
- Result: **97 passed, 2 skipped** (PB-7 auto-skip ×2, non-HITL). `scripts/` scaffold
  self-regression: 216 passed (the single `test_gitlab_ci_stage_matrix` failure is a
  pre-existing scaffold path quirk — the scaffold manifest is distributed separately,
  gitignored and therefore absent —
  identical on released templates; not run by CI `run-tests` which scopes to `tests/`).

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | 外部 CLI 経由のモデルを `BaseLLM` 実装としてラップして注入 |
| 注入シーム | `live_llm_injector` |
| 実 LLM 呼び出し | 2 回 |
| 出力 | ['mode', 'provenance_verified', 'gap_findings', 'review_questions', 'notice'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（外部 CLI と鍵が要るため）。
