# SVC-C2-132 — Game Live-Ops Player-Promise Impact Review Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

This agent reviews a limited-time game event **before** it ships. It reads the player-facing
notice next to the versioned event configuration that will actually run — the reward table, the
eligibility rules, the change request — and reports where the two diverge in meaning: who is
eligible, what probability is implied, what caps and deadlines apply, when rewards actually land,
which approvals are missing, and which players are affected.

The interesting failures in live-ops are not ID mismatches. They are sentences like *"available
any time during the event"* sitting on top of rules that say *once per day, stock-capped,
distributed the next morning*. Checking IDs and versions is deterministic and the agent does it,
but as a subordinate step; the judgement that a promise and a rule mean different things is what
the language model is there for.

The design is shaped by what happens when such a review is wrong:

- **Findings must be traceable to their source.** Each one carries citations with a verbatim
  quote (minimum length enforced), a source identifier that must actually resolve, and a
  bidirectional numeric check — a figure in a finding has to match the figure in the source, and
  a unit token sitting next to a number cannot slip a claim past the guard.
- **The agent does not make legal determinations.** A closed lexicon blocks categorical
  legal-conclusion phrasing in the free text the agent itself writes. This is a product-safety
  and non-authoritative-output control; it does not establish compliance with advertising or
  loot-box disclosure regulation, and this template does not claim to.
- **Recorded past decisions are facts, not leaks.** Recall from the adjudication knowledge base
  is namespaced (`past_verdict` / `past_rationale`) so a previous ruling can be shown without
  colliding with the current case's own verdict, and quoted evidence paths are exempt from the
  assertive-language scan rather than being scrubbed of the very text they exist to reproduce.
- **Recall never feeds the next prompt.** Free-text recalled from the knowledge base goes only to
  a human-facing reference block, never into the judgement model's input — which removes the
  "stored text becomes an instruction" injection path structurally rather than by filtering.
- **Large inputs stay out of checkpoints.** An event configuration above 256 KiB must be passed as
  an opaque `event_config_ref` handle instead of inline; an oversized inline body is rejected.

Every write is scoped and every input is a closed schema — unknown keys and wrong types are
rejected, not ignored.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- a `BaseLLM` implementation installed through the module seam
  `src.services.llm_seam.set_llm()`. It is a module-level seam rather than a constructor argument
  because every node must stay no-argument constructible. **No client class and no model id exist
  in this template**; with nothing injected the nodes degrade to a deterministic fallback, which
  produces a structurally valid but semantically empty review;
- a caller-authorization channel the client cannot forge (below);
- caller authentication in front of the entry adapter.

`config/agent.yaml` declares `requires.secrets: []` and `requires.extras: []`. Runtime parameters
come from `config/config.yaml`, which the standalone server does load and pass to the graph:

| Key | Default | Read by |
|---|---|---|
| `max_input_bytes` | `262144` | Input gate — the inline/`event_config_ref` threshold |
| `max_field_chars`, `max_bad_char_ratio` | `20000`, `0.1` | Input gate — free-text limits |
| `provenance_min_quote_len` | `8` | Provenance verification — the shortest acceptable verbatim quote |
| `kb_recall_top_k` | `8` | Knowledge-base recall |
| `kb_raw_retention_days` | `365` | Raw-text retention in the knowledge base. **A placeholder to calibrate at deployment**, validated fail-closed at load (zero or non-integer is rejected) |
| `max_retry`, `timeout_s` | `3`, `30` | Framework routing |
| `memory_enabled`, `hitl.enabled` | `false`, `false` | No checkpointer, no human-in-the-loop pause |
| `llm_model`, `temperature`, `max_tokens`, `budget_usd` | — | **Validated at load, not consumed.** This template constructs no client, so the injected one owns those choices; the keys exist so a deployment's intent is recorded and checked |

## Input

There is one HTTP route, `POST /invoke`, taking `input` (a JSON string) and an optional
`session_id`. The **mode** lives inside that JSON, not in the URL:

| Mode | Access | Payload | Returns |
|---|---|---|---|
| `invoke` | read | `event_config` (or `event_config_ref`), `reward_table`, `change_request`, `notice_text`, `tenant_id`, `org_id`, optional `context` | The exception packet — source-linked findings plus questions for the human reviewer |
| `ingest` | write | `records` (past notice corrections and their adjudications), `tenant_id`, `org_id` | Ingested count |
| `feedback` | write | `case_id`, `manager_action` (`corrected` / `accepted` / `rejected`), `final_verdict`, `tenant_id`, `org_id`, optional `correction_note` | Recorded result |

Any other mode value is rejected before the workflow runs. Each mode has a closed schema: a
missing required key, an unknown key, or a wrong type is a rejection, not a warning.

### Authorization is two independent gates

**Caller authentication.** When `INVOKE_AUTH_TOKEN` is set on the server environment, a caller
that upstream middleware has not vouched for must present it as `Authorization: Bearer`; a
mismatch is **401**. Every node requires `VERIFIED_EXTERNAL`, so a caller who reaches the graph
without that trust is stopped by the framework's trust gate and receives `status: error`.

**Scope authorization.** The requested `tenant_id` / `org_id` must fall inside the caller's
authorized scopes, with `read` for `invoke` and `write` for `ingest` / `feedback`. Those scopes
are resolved **only** from a channel a remote client cannot forge, in priority order:

1. `request.state.authorized_scopes`, set in-process by platform auth middleware;
2. an `X-Authorized-Scopes` header verified by HMAC-SHA256 against
   `X-Authorized-Scopes-Signature` under a shared `SCOPE_SIGNING_KEY`.

A raw, unsigned `X-Authorized-Scopes` header is **never** trusted. The gateway still owns JWT
verification and scope derivation; the signature only proves channel integrity, so the platform's
trust model is not forked here. `SCOPE_SIGNING_KEY` is a deployment-level channel credential read
from the environment before any invocation context exists — the same class as `INVOKE_AUTH_TOKEN`,
not an agent secret, and never stored in state.

With no trusted source of scopes, the resolved set is empty and **every tenant and org is denied**.
That is the intended standalone behaviour: an authenticated request with no gateway signature and
no middleware comes back `status: error`, not a review.

```bash
curl -X POST http://localhost:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -H "X-Authorized-Scopes: ${SCOPES_JSON}" \
  -H "X-Authorized-Scopes-Signature: ${SCOPES_HMAC}" \
  -d '{"session_id": "review-001", "input": "{\"mode\":\"invoke\",\"tenant_id\":\"t1\",\"org_id\":\"o1\",\"event_config\":{…},\"reward_table\":{…},\"change_request\":{…},\"notice_text\":\"…\"}"}'
```

The Marketplace adapter (`cli.py`) is deliberately narrower: it forces `mode: invoke`, rejects
any other mode, takes the tenant and org from `ASTAR_TENANT_ID` / `ASTAR_ORG_ID` with `read`
permission only, and installs an Azure client into the language-model seam. `ingest` and
`feedback` are unreachable through it by construction — the facade cannot reach the trust level a
write requires, so lowering the write check to make them reachable would be exactly wrong.

## Output

The AgentCore envelope is returned — `output`, `status`, `trace_id`, `correlation_id`,
`node_history`. For a review, `output` is the exception packet: the findings, each with its
citations (source identifier, version, verbatim quote), the affected-beneficiary and
version evidence, and the questions a human reviewer should answer. `ingest` and `feedback` return
their record counts instead.

Findings are advisory input to a human decision. The packet is written to be reviewed, not
actioned automatically.

## Security and Limitations

Every node declares `VERIFIED_EXTERNAL` trust; caller authentication, the unforgeable-scope RBAC
check and the framework trust gate are three separate controls, and none of them is documentation
alone — each is enforced in code and covered by a negative test (a forged scope header must not
authorize).

Provenance verification runs inside the producing node's `execute()`, not in the output-gate hook,
because verifying a citation needs the source registry that lives in state and the hook does not
receive it. The same placement rule applies to redaction: content is cleaned where it is produced,
before the gate sees it.

Known limits, stated plainly:

- **Not a compliance or legal determination.** The assertive-language lexicon is a product-safety
  control on the agent's own wording. It does not establish conformity with advertising or
  loot-box disclosure rules, and the EU AI Act Annex III declaration does not apply to this
  industry category.
- **The agent does not act.** It deploys no configuration, grants no items, touches no player
  account, sends no player communication, and decides no compensation.
- **Verification is not comprehension.** A matched quote proves the text appears in the source; it
  does not prove the finding built on it is correct.
- **The knowledge base is scoped and retained.** Raw text has a configured retention window
  (365 days by default, a placeholder to calibrate) after which it is redacted; the tenant/org
  isolation of that store is a boundary the deployment's own credentials must also enforce.
- **Without an injected model the review is empty.** The deterministic fallback keeps the shape,
  not the judgement.

Output is machine-generated and assistive. A live-ops manager, not the agent, decides what ships.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), outer graph with an inner workflow subgraph
- Generation mode: `llm` (degrades deterministically when no model is injected)
- HITL: not enabled — review happens downstream of the output, and the result is recorded through
  the `feedback` mode as a separate invocation
- Persistence: no checkpointer (`memory_enabled: false`); the adjudication knowledge base is its
  own store
- Required trust level: `VERIFIED_EXTERNAL`

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built to run on the platform, which supplies the caller authentication, the authorization scopes and the language-model client. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when the platform is absent — but it does start. The standalone server loads
`config/config.yaml`, compiles the graph and provisions its secret provider at import, and
`/health` returns `{"status": "ok"}`. What it will not do is produce a review. With
`INVOKE_AUTH_TOKEN` set, an unauthenticated request is **401**; an authenticated one with no
gateway-signed scopes and no auth middleware resolves to an empty scope set, the tenant/org check
denies it, and the response is `status: error` with `output: null`. Failing closed on
authorization is the point: a review of another tenant's event is worse than no review. The test
suite runs without a platform connection.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Documentation

`docs/02_design.md` carries the node flow, the mode contracts and their closed schemas, the
provenance and lexicon design, the two-layer data governance for the knowledge base, and the
security design including the RBAC trust boundary. `docs/03_test_spec.md` records the test
specification and the verification results.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
