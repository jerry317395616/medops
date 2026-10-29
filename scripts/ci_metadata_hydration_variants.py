"""CI-only fresh bulk hydration experiment; never grants financial authority."""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

from ci_native_reservation_desk_probe import connect, snapshot


def raw_documents(frappe, names):
    from frappe.model.base_document import TABLE_DOCTYPES_FOR_DOCTYPE
    names = tuple(sorted(set(names)))
    if not names or len(names) > 256:
        raise AssertionError("bounded metadata names required")
    rows = frappe.get_all("DocType", filters={"name": ("in", names)}, fields=["*"], limit_page_length=0)
    documents = {row.name: {**dict(row), "doctype": "DocType"} for row in rows}
    if set(documents) != set(names):
        raise AssertionError("complete metadata required")
    for field, child in TABLE_DOCTYPES_FOR_DOCTYPE.items():
        for document in documents.values():
            document[field] = []
        for row in frappe.get_all(child, filters={"parent": ("in", names), "parenttype": "DocType", "parentfield": field}, fields=["*"], order_by="parent asc, idx asc", limit_page_length=0):
            documents[row.parent][field].append(dict(row))
    return documents


def hydrated_metadata(frappe, names):
    return {name: frappe.get_meta(frappe.get_doc(row), cached=False) for name, row in raw_documents(frappe, names).items()}


def official_bulk_documents(frappe, names):
    from frappe.model.document import get_docs
    return {doc.name: frappe.get_meta(doc, cached=False) for doc in get_docs("DocType", filters={"name": ("in", sorted(names))}, chunk_size=256)}


def raw_loaded_native_meta(frappe, names):
    # Experimental exact pinned constructor state; native processing is unchanged.
    # No production adoption without full equivalence/freshness/permission review.
    from frappe.model.meta import Meta
    from frappe.model.base_document import BaseDocument
    result = {}
    for name, row in raw_documents(frappe, names).items():
        meta = Meta.__new__(Meta)
        meta.doctype = "DocType"
        meta.name = name
        meta.flags = frappe._dict(for_update=False)
        BaseDocument.__init__(meta, row)
        meta.process()
        frappe.client_cache.set_value(f"doctype_meta::{name}", meta)
        result[name] = meta
    return result


def differences(left, right, prefix="", limit=48):
    out = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                value = right[key] if key in right else left[key]
                out.append({"path": prefix+"/"+key, "kind": "added" if key in right else "removed", "is_null": value is None, "type": type(value).__name__})
            elif left[key] != right[key]:
                out.extend(differences(left[key], right[key], prefix+"/"+key, limit))
            if len(out) >= limit:
                break
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            out.append({"path": prefix, "kind": "length", "native": len(left), "candidate": len(right)})
        for index, (old, new) in enumerate(zip(left, right)):
            if old != new:
                out.extend(differences(old, new, prefix+"/"+str(index), limit))
            if len(out) >= limit:
                break
    elif left != right:
        out.append({"path": prefix, "kind": "value_or_type", "native_type": type(left).__name__, "candidate_type": type(right).__name__})
    return out[:limit]


def native_metadata(frappe, names):
    return {name: frappe.get_meta(name, cached=False) for name in sorted(names)}


def measure(frappe, function, names):
    count = 0
    original = frappe.db.sql
    def counted(*args, **kwargs):
        nonlocal count
        count += 1
        return original(*args, **kwargs)
    started = time.perf_counter()
    with patch.object(frappe.db, "sql", counted):
        result = function(frappe, names)
    return result, {"seconds": round(time.perf_counter() - started, 6), "sql_calls": count, "meta_count": len(result)}


def normalized(frappe, metas):
    return {name: json.loads(frappe.as_json(meta.as_dict())) for name, meta in metas.items()}


def main():
    from medops.tests.disposable_reservation_site import require_disposable_reservation_site
    from medops.hrp_budget.services.release_execution import AUTHORITY_ROOTS
    output = Path(os.environ["RUNNER_TEMP"]) / "metadata-hydration-evidence"
    output.mkdir()
    result = {"status": "failed", "product_financial_authority_code_changed": False,
              "scope": "Fresh primary-transaction raw DocType child hydration through native Document/Meta. Metadata equivalence/latency experiment only; not financial authorization/revocation/production acceptance."}
    frappe = connect(Path(os.environ["BENCH_DIR"]).resolve())
    require_disposable_reservation_site(empty=True)
    before = snapshot(frappe)
    stage = "native_graph"
    try:
        frappe.db.rollback()
        frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        frappe.db.begin()
        names = set(AUTHORITY_ROOTS)
        pending = set(names)
        while pending:
            metas = native_metadata(frappe, pending)
            nested = {field.options for meta in metas.values() for field in meta.fields if field.fieldtype in {"Table", "Table MultiSelect"} and field.options}
            pending = nested - names
            names |= pending
            if len(names) > 256:
                raise AssertionError("metadata graph too large")
        stage = "compare_native_variants"
        result["variants"] = {}
        functions = {"document_clone": hydrated_metadata, "official_get_docs_clone": official_bulk_documents, "pinned_raw_native_constructor": raw_loaded_native_meta}
        for label, function in functions.items():
            native, baseline = measure(frappe, native_metadata, names)
            candidate, cost = measure(frappe, function, names)
            old, new = normalized(frappe, native), normalized(frappe, candidate)
            mismatch = [name for name in names if old[name] != new.get(name)]
            result["variants"][label] = {"native": baseline, "candidate": cost, "mismatch_count": len(mismatch), "differences": {name: differences(old[name], new.get(name)) for name in sorted(mismatch)[:3]}}
        selected = functions["pinned_raw_native_constructor"]
        if result["variants"]["pinned_raw_native_constructor"]["mismatch_count"]:
            raise AssertionError("exact native metadata equivalence not established")
        observations = []
        for index in range(3):
            native, baseline = measure(frappe, native_metadata, names)
            bulk, candidate = measure(frappe, selected, names)
            if normalized(frappe, native) != normalized(frappe, bulk):
                raise AssertionError("repeated equivalence mismatch")
            observations.append({"native": baseline, "hydrated": candidate})
        result["steady_state"] = observations
        stage = "own_transaction_changes"
        cases = []
        # Own uncommitted configuration mutations; rolled back, no schema change.
        for table, field, target in (("DocField", "permlevel", 1), ("DocPerm", "read", 0)):
            frappe.db.savepoint("metadata_probe")
            row = frappe.get_all(table, filters={"parent": "HRP Hospital", "parenttype": "DocType", field: ("!=", target)}, fields=["name"], limit_page_length=1)
            if not row:
                raise AssertionError("native mutation fixture absent")
            previous = normalized(frappe, native_metadata(frappe, {"HRP Hospital"}))
            frappe.db.set_value(table, row[0].name, field, target, update_modified=False)
            expected = normalized(frappe, native_metadata(frappe, {"HRP Hospital"}))
            actual = normalized(frappe, selected(frappe, {"HRP Hospital"}))
            if expected != actual or actual == previous:
                raise AssertionError("own metadata mutation was missed")
            cases.append({"kind": table, "own_uncommitted_change_observed": True})
            frappe.db.rollback(save_point="metadata_probe")
            if normalized(frappe, selected(frappe, {"HRP Hospital"})) != previous:
                raise AssertionError("metadata rollback was missed")
        result["own_transaction_cases"] = cases
        frappe.db.rollback()
        after = snapshot(frappe)
        if before != after or frappe.db.count("Error Log"):
            raise AssertionError("read-only financial evidence changed")
        result.update(status="passed", immutable_financial_snapshots_identical=True, error_log_count=0, metadata_count=len(names))
        return 0
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        result["failed_stage"] = stage
        import traceback
        result["static_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
        return 1
    finally:
        frappe.db.rollback()
        frappe.destroy()
        (output/"metadata-hydration.json").write_text(json.dumps(result, indent=2)+"\n")
        print(json.dumps({"status": result["status"], "scope": result["scope"]}))

if __name__ == "__main__":
    raise SystemExit(main())
