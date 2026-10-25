# Budget planning — COD-031

## Delivered boundary

The HRP Budget module implements compilation versions and department plans. Three typed master-data prerequisites supply business dimensions, dimension values and funding sources. This slice is under validation; hospital production readiness is not established.

Use `/desk/hrp-budget-planning` from the HRP workspace. Select a company and hospital, maintain budget classifications as a data steward, then create a version as a budget manager. Choose a published organization snapshot and an ERPNext Fiscal Year. Create department plans, enter classification/fund/account/project/period amounts, or use exact period allocation. A version retains the native fiscal dates, company currency and currency precision captured at creation.

Freeze validates every included plan, including current sources and business rules. It seals compilation facts and leaves department plans at `docstatus=0`. It does not approve finance, create available budget, reserve funds or post accounting/stock ledgers. Copy a frozen version to revise; withdrawn plans remain in history and are excluded from the new draft. COD-032 through COD-035 implement subsequent authorization and execution boundaries.

## Permissions and retry behavior

Installation creates role definitions and indexes only. No user gains a role, hospital scope, ERP permission or organization share. HRP Budget Manager can maintain versions, freeze and clone; Budget User and Finance User can maintain scoped plans. Data Steward controls classifications. Auditor has read access subject to current scope and native sources. Operators also need explicit read permission to hospital, organization snapshot/unit/mapping and native ERP references. Export requires current native export permissions throughout the source chain.

Generic lists fail closed for ordinary users. The page uses bounded facade lists and a fixed-DocType link query, checking every candidate before pagination. Five owned planning/master DocTypes use friendly titles. A narrow override of the native title RPC checks current owned document, source and field permissions; other DocTypes retain native behavior and guest access remains blocked. Native link validation uses one authorized exact-name lookup instead of accepting its requested 25,000-candidate scan. Field masking and higher field permission levels apply to plan headers, lines and native reference fields.

POST methods FIN-014 through FIN-019 require an `Idempotency-Key` header and strict command payloads. An identical successful retry returns only its original reference/revision receipt after current authorization. Source disablement prevents fresh writes but does not invalidate that receipt. Reads return current authorized values separately. The page retains a request key while the submitted payload is identical; changes create a new key. Optimistic revisions and hospital/version locking serialize conflicting writes.

## Money, integrity and bounded work

Authoritative amounts are canonical decimal strings stored in Data columns and calculated with Decimal, avoiding the pinned Frappe Currency-to-float persistence path. Fractional API amounts must be strings: already-decoded binary floats are rejected because their original decimal digits cannot be recovered. Exact bounded integers and internal Decimal values are also supported. No precision is silently rounded. Only the two reviewed budget amount fields become numeric business-rule sources. Equal period allocation conserves the exact total and assigns remainder minor units to earlier periods. Native budget calendar/account rules are reused without saving native Budgets.

Plan grain is version, organization snapshot/unit, budget subject, funding source, account, optional project and fiscal period. Native cost center mapping is captured and cannot silently change on revision or cloning. Referenced native Company, Currency, Fiscal Year, Cost Center, Account and Project renames/merges are blocked pending a reviewed data migration. Native rename and domain writers share the source row fence; reference checks use current locking reads so an old MVCC snapshot cannot miss a concurrently committed reference. Master-data writers also fence the native company row. Service guards prohibit direct document mutation, submission, deletion or cancellation of controlled planning records.

Limits: 5,000 rows per plan, 1,000 plans per version, 50,000 rows per freeze/clone operation, bounded typed payload hashes and 5,000 list candidates. These are safety limits, not measured hospital capacity. Larger operational scale requires capacity evidence and a reviewed staging design.

## Upgrade and acceptance

Migrate creates six existing blueprint models as real DocTypes, adds explicit role definitions and eleven indexes/unique constraints, and refreshes the app-owned HRP workspace. It creates no budget or financial seeds. Run migration twice in the private validation bench before release. Verify decimal persistence, idempotent retries, rollback of denied writes, native reference protections, scope/source/field/export revocation, actual HTTP contracts, freeze/save concurrency and browser operation. Full-suite, protected CI and hospital acceptance remain separate release gates.

Back up database/files before deployment. Roll back application code under maintenance while preserving all new tables, exact amounts, revisions, snapshots and audit evidence. Never delete planning data to roll back a release or disable source checks. A frozen version is compilation history; do not reinterpret it as financial approval during a rollback or later migration.
