# Budget submission, aggregation and human approval — COD-032

This slice is in progress. Budget planning is delivered; submission and financial approval are not yet delivered. ADR-0029 defines the intended boundary.

Department submissions seal facts from a frozen plan. Consolidation selects approved department submissions within the same company, hospital, published organization snapshot, fiscal year, currency and precision. Each line retains its original plan/line, unit, account, budget subject, funding, project and period. Duplicate original facts and nested consolidations are rejected.

Approval cases are separate from financial snapshots. A case captures the matrix decision, delegation evidence and source digest. Human decisions advance sequential All/Any steps and create immutable review records. Rejection followed by a new request creates another case round. Direct model writes and native workflow calls cannot bypass the governed adapter. An approval does not create an available budget or post ledgers.

Matrix version1 keeps its two-decimal currency column, original serialization, digests and historical receipts. Version2 captures0–9 precision, stores thresholds as exact decimal strings in a separate column and rejects binary floats. New evaluations compare actual captured source precision and exact source values. Their decision digest binds the amount and context. Legacy policies retain their digest when reused with exact evaluations; exact policies do not change version1 evaluation behavior. New exact budget approval targets require the exact evaluation contract.

Current work begins with the precision contract and migration compatibility. Typed submission models, guarded services, human confirmation, source authorization, aggregation, workflow/ToDo adaptation, APIs, UI and concurrency verification remain required before completion. Installation will define permissions and workflows without assigning user roles, scopes or shares.
