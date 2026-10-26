# Budget submission, consolidation and human approval — COD-032

COD-032 is a verified development release. PR45 was protected-squash-merged as22dc042, with its complete tree identical to verified818f9e4. Both isolated medical development/test sites were backed up, migrated twice and verified. [Release evidence](releases/COD-032.json) records the exact scope. Full HRP and hospital production readiness remain incomplete.

## Financial facts and money

The slice implements the four existing Submission, Submission Line, Approval and Review blueprints. Sealing freezes financial facts; approval uses a separate case and immutable events. Neither creates available budget, native ERP Budget, GL Entry or Stock Ledger Entry. Allocation and execution remain COD-033–035.

Department schema1 copies a complete frozen plan, preserving original plan/line, unit, cost center, account, budget subject, funding, project, period and captured precision. One original plan has one sealed snapshot. Rejected approval rounds reuse it. Hospital/version locks protect an internal recomputation of the bounded frozen version proof; other departments are not returned or granted. Whole-version digests stay private in parent/child and are omitted from public snapshots. Optional display labels require current field permission, fall back to authorized identifiers and never change facts or digests.

Consolidation schema2 accepts approved department leaves in the same company, hospital, published organization snapshot, year, currency and0–9 captured precision. Each reference binds its approved case round/revision, decision digest and final immutable review digest, plus original preparers/applicants/requestors. Every original line remains intact. The root must contain all units in that tree; header cost_center is null while original centers stay distinct. Nested aggregates, duplicate original plans/facts and overlapping periods at the same unit/center/subject/fund/account/project grain are rejected; disjoint periods remain separate.

Pending proposals may overlap for comparison. Before final approval, the hospital fence and current locked lookup ensure one approved consolidation per original plan. The check precedes Decision insertion and native Workflow conversion, so a conflict rolls back the whole decision. Rejected/withdrawn/pending proposals do not claim sources. Identity binds root and approved source set, preventing duplicate proposals through changed subjects/dates.

Amounts persist, calculate and display as bounded exact decimal strings, supporting15 integer digits and up to9 captured fractional digits. Matrix schema1 preserves its two-decimal Currency column, serialization, digests and durable receipts. Schema2 uses a separate Data threshold and compares actual source amount/precision without binary floats. Existing matrices receive version1/precision2 defaults without rewriting old thresholds or evidence. Exact evaluations bind amount, precision and context; exact policies do not alter legacy evaluation behavior.

## Access and human decisions

Reads, titles, lists, exports, replays and actions check current roles, hospital/unit scope, all selected sources and their fields. Sources include original compilation, sealed departments, approved cases and immutable decisions. One unreadable source denies the whole snapshot; visible-only row filtering or totals are forbidden. Budget Approver has read-only metadata plus governed capability, with no native financial create/write/submit/cancel. Installation assigns no user roles, scopes or shares.

All original preparers, department applicants/requestors and consolidation applicants/requestors are forbidden from approving the consolidation. Principal/delegate resolution cannot bypass separation. Referenced user identifiers cannot be renamed through normal native APIs; disabling an account invalidates its next action. Sequential All/Any steps, rejection/new rounds and withdrawal preserve old evidence. One submission has one live case.

Financial commands require a normal Cookie session and CSRF, rejecting Authorization headers and command-line decisions. Prepare persists a Confirmation bound to actor, source/policy/segregation/revision, session HMAC and300-second expiry. Random proof stays in page memory; only its hash persists. Explicit Boolean acknowledgement consumes the same-session proof and appends a Decision. Unique confirmation/vote/revision keys prevent duplicate decisions. Later actions and durable replay check current access; policy/delegation changes require withdrawal and a new round.

The fixed native Workflow accepts no user expression, script or webhook. Optional states suppress global Workflow Action creation. Controlled ToDos follow the current step and create no shares; generic lists exclude these records, while the approval facade checks full sources. Direct saves, submit/cancel, native Workflow and background submission cannot bypass capability guards. Notifications retain the existing Outbox/after-commit boundary.

Repeated source checks reuse only one synchronous read-only access graph, separated by user, export mode and fields. ContextVar state clears on exit and exceptions. Independent reads, locked reloads, replay and later actions start fresh; commands/mutations never share the graph, and locked source proof retrieval does not reuse unlocked results. Five lifetime/thread tests and native revoke/field/confirmation regressions verify this boundary.

## APIs, pages and limits

FIN-025–027 seal/read/list submissions, FIN-028–032 request/prepare/confirm/read/list cases, FIN-033 seals consolidation and FIN-034 lists approved leaves. Read responses distinguish schema2 while department schema1 and receipts stay compatible. API/OpenAPI catalogs specify bounded filters and exact strings.

Planning opens sealed department facts and approval. The plural hrp-budget-approvals and hrp-budget-consolidations pages avoid native DocType slug collisions. Consolidation supports paged selection, original detail, source-case links and independent review. Owned v17 sidebar_items expose all three pages. Normal migrations import Page metadata so boot.page_info invalidates cached scripts. Text rendering and ordered fiscal periods retain exact money without Number conversion.

Compilation is bounded to1000 plans/50000 total lines, departments to5000 lines, and consolidation to1000 sources/50000 lines,2MiB source-reference JSON and64MiB streamed canonical facts. Shared JSON/schema1 limits remain unchanged. Lists cap5000 candidates/100 returned rows and fail closed on overflow. Policies filter dates, ancestry and dimensions before their100 relevant-candidate bound; damaged/unknown ancestry fails closed. Bounds and synthetic measurements do not establish hospital capacity.

## Verification, deployment and rollback

Protected CI40263234479 passed all four required jobs at818f9e4:380 pure tests,360 complete Frappe tests and zero Error Logs on each of three fresh sites. All121 focused native/HTTP tests and two final private migrations passed with clean source. Upgrade baselines preserve legacy requests, policies, decisions and durable receipts.

Actual independent browser sessions completed department two-person All approval and consolidation Any approval. The consolidation kept two units/centers and exact100000.000000001; requestor refresh and post-approval detail trace passed. One confirmation and one decision persisted, with no open ToDos, case shares or global Workflow Action. This is synthetic development acceptance, not hospital UAT.

Seven overlapping dual-process HTTP races at818f9e4 passed with normal CSRF: same/different-key consolidation sealing, overlapping final approvals, same/different-key confirmation, different actors at one revision and concurrent case requests. A508 abort retries the whole fresh HTTP request with the original proof/key. Same keys yield one create plus one replay; conflicts yield one success and one409. Losing overlapping proposals have no Decision. Ledger/share counts do not change; services never retry inside a transaction.

Both running medical sites received database/configuration/public/private-file backups and two migrations to22dc042. Original business columns/values/timestamps, full user/profile role assignments and the test site's legacy matrix payload were preserved. Four new financial models remained empty. Backup reconstruction proved that metadata differences were only45 regenerated report/I-ONE child identifiers/timestamps and four I-ONE Role timestamps; permission values and other fields match exactly. Owned page/workspace roles match source. Normal native sign-in may update session timestamps.

All21 budget endpoints reject guests with401. Authenticated pages/lists, health, pinned versions, ione_core, workbench, Socket.IO and public HTTPS pass; Error Logs do not increase. Source/dependencies stay clean. Other41 service PIDs/states and child/rehab health match the baseline; only medical Web/workers/scheduler were paused/restored.

Rollback requires maintenance while retaining exact columns, models, sealed facts, confirmations and decisions. Never delete financial evidence or interpret schema2 with an old two-decimal path. Keep incompatible actions closed until a verified recovery. Remaining36-module functions, hospital scale, actual interfaces, UAT and disaster recovery stay explicit production gates.
