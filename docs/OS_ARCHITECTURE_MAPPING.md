# ForgeOS OS Architecture — Spec-to-Schema Mapping

Date: 2026-10-09. Maps the "business operating system" spec (§4–§12) to the
actual ForgeOS schema. Rule: map to what exists; don't duplicate tables.

## §4 Shared platform tables → actual models

| Spec entity              | ForgeOS model(s)                              | Status                          |
|--------------------------|-----------------------------------------------|---------------------------------|
| organizations            | `Business`                                    | ✅ exists                       |
| organization_memberships | `User.business_id`                            | ✅ exists (simple, no join tbl) |
| roles and permissions    | `UserRole`, `require_role()`                  | ✅ exists                       |
| business_events          | `Event`                                       | ✅ exists                       |
| workflow_definitions     | —                                             | ❌ missing                      |
| workflow_runs            | — (alpha lead runs are domain-specific)       | ❌ missing                      |
| workflow_steps           | —                                             | ❌ missing                      |
| agent_definitions        | hardcoded in `draven_swarm.py`                | ⚠️ code, not DB                 |
| agent_runs               | `DravenSwarmRun`, `DravenSwarmEvent`           | ✅ exists                       |
| approval_requests        | `AssetApproval`, `AlphaApproval`              | ✅ exists (two domain-specific) |
| integration_connections  | `BusinessSecret` (vault)                      | ⚠️ secrets yes, metadata no     |
| audit_logs               | `DravenToolRun`, `Event`                      | ⚠️ partial                      |
| knowledge_documents      | `KnowledgeDoc`                                | ✅ exists (new)                 |

## §4 Core entities → actual models

| Spec entity              | ForgeOS model(s)                              | Status                          |
|--------------------------|-----------------------------------------------|---------------------------------|
| Organization             | `Business`                                    | ✅                              |
| Users and roles          | `User`, `UserRole`                            | ✅                              |
| Customers and contacts   | `Contact`                                     | ✅                              |
| Products and services    | —                                             | ❌ missing (no catalog)         |
| Leads and opportunities  | `Opportunity`, `LeadSource` (+alpha)          | ✅                              |
| Orders / financials      | `StripeOrderEvent` (raw ingest only)          | ⚠️ ingest yes, ledger no        |
| Tasks and workflows      | —                                             | ❌ missing (no tasks table)     |
| Agents and tools         | swarm agents + `TOOLS` registry               | ✅ (code-level)                 |
| Events and audit records | `Event`                                       | ✅                              |
| Knowledge and policies   | `KnowledgeDoc`                                | ✅                              |

## Honest gap list (what the OS vision still needs)

1. **Products/services catalog** — no table. Needed before orders mean anything.
2. **Tasks** — no `tasks` table (assignments, dependencies, due dates). The
   dashboard blueprint and "daily briefing" both assume this.
3. **Workflow engine** (Automation Studio §5) — no `workflow_definitions` /
   `workflow_runs` / `workflow_steps`. The alpha lead-run state machine and
   the campaign step engine are domain-specific; neither is a general engine.
4. **Unified approvals** — `AssetApproval` and `AlphaApproval` are separate;
   the spec wants one `approval_requests` table.
5. **Financial ledger** — `StripeOrderEvent` ingests raw orders; no
   deterministic ledger, no invoices/expenses (Growth Engine P3 territory).

## What NOT to rebuild

The spec's Phase 1 ("Foundation: identity, data and control plane") is
~80% done: tenant isolation (`business_id` scoping everywhere), roles,
encrypted vault, event log, agent runs, approvals (domain-specific), API +
worker + Postgres + Alembic + Fly/sprite deploys. Do not re-architect this.

## Recommended sequencing (per spec §9, adjusted to reality)

- **Now:** Products catalog + Tasks (small tables, unlock the dashboard).
- **Next:** Unify approvals → single `approval_requests` table.
- **Then:** Automation Studio workflow engine (the big one — §5).
- **Later:** Financial ledger (Growth Engine P3), module marketplace (§6).

## §6 Universal command interface

Draven IS this interface (voice + text, 68 tools, approvals, execution
history via `DravenToolRun`). Gap: no persistent task status view yet —
needs the Tasks table above.

## §7 Autonomy levels

Current mapping: L1 (read tools) ✅, L2 (draft) ✅, L3 (approval-gated
launch/approve) ✅, L4 (bounded autonomy) ❌ — no policy framework for
auto-execution exists. Do not build L4 until the audit trail + rollback
story is solid.
