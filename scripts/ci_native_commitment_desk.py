"""Real Chromium consent/post/reload, only on a fresh named disposable CI site."""
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

SITE = "hrp-reservation-concurrency.localhost"
PORT = 18137
BASE = f"http://{SITE}:{PORT}"
PREFIX = "medops.api.v1.finance.reservation_operations."
TABLES = (
    "HRP Budget Reservation", "HRP Budget Reservation Event",
    "HRP Budget Commitment Authorization", "HRP Budget Reservation Authorization",
    "HRP Budget Reservation Renewal", "HRP Budget Reservation Release",
    "GL Entry", "Stock Ledger Entry", "HRP Budget Ledger",
    "Material Request", "Material Request Item", "Purchase Order", "Purchase Order Item",
)


def connect(bench):
    import frappe
    from medops.tests.disposable_reservation_site import require_disposable_reservation_site
    frappe.init(site=SITE, sites_path=str(bench / "sites"))
    frappe.connect()
    require_disposable_reservation_site()
    if frappe.conf.get("ignore_csrf"):
        raise RuntimeError("native CSRF protection must be enabled")
    frappe.set_user("Administrator")
    return frappe


def snapshot(frappe):
    result = {}
    for table in TABLES:
        rows = frappe.get_all(table, fields=["*"], order_by="name", limit_page_length=2001)
        if len(rows) > 2000:
            raise AssertionError("bounded CI fixture required")
        result[table] = {row.name: row for row in rows}
    return result


def safe_snapshot(frappe, values):
    return {table: {"count": len(rows), "sha256": hashlib.sha256(frappe.as_json(rows).encode()).hexdigest()}
            for table, rows in values.items()}


def serve():
    bench = Path(os.environ["BENCH_DIR"]).resolve()
    if Path.cwd().resolve() != bench / "sites":
        raise RuntimeError("disposable sites directory required")
    connect(bench).destroy()
    import frappe.app
    from werkzeug.serving import run_simple
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    run_simple("127.0.0.1", PORT, frappe.app.application_with_statics(),
               threaded=True, use_reloader=False, use_debugger=False)


def main():
    bench = Path(os.environ["BENCH_DIR"]).resolve()
    output = Path(os.environ["RUNNER_TEMP"]) / "native-commitment-desk-evidence"
    output.mkdir()
    process, page = None, None
    stage = "containment"
    evidence = dict(status="failed", page_errors=0, failed_native_requests=[], posts=0,
                    include_core=os.environ.get("INCLUDE_IONE_CORE", "0"),
                    candidate=os.environ["GITHUB_SHA"], server_touched="disposable GitHub CI only")
    try:
        frappe = connect(bench)
        from medops.tests.test_budget_consolidation import MANAGER
        from frappe.utils.password import update_password
        from frappe.desk.page.setup_wizard.setup_wizard import disable_future_access
        user = frappe.get_doc("User", MANAGER)
        if user.user_type != "System User" or not user.enabled or "System Manager" in frappe.get_roles(MANAGER):
            raise AssertionError("ordinary enabled synthetic manager required")
        update_password(MANAGER, os.environ["ADMIN_PASSWORD"])
        frappe.get_single("Installed Applications").update_versions()
        disable_future_access()
        frappe.clear_cache()
        frappe.db.commit()
        if not frappe.is_setup_complete():
            raise AssertionError("native setup not complete")
        before = snapshot(frappe)
        roots = sorted(before["HRP Budget Reservation"])
        if not 1 <= len(roots) <= 10:
            raise AssertionError("completed dedicated cookie fixture required")
        frappe.destroy()
        stage = "loopback_server"
        with (bench / "logs" / "commitment-desk-private-server.log").open("w") as private_log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve"],
                                       cwd=bench / "sites", stdout=private_log, stderr=private_log)
        deadline = time.monotonic() + 60
        while True:
            try:
                connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
                connection.request("GET", "/login", headers={"Host": SITE})
                response = connection.getresponse()
                ready = response.status == 200
                response.read()
                connection.close()
                if ready:
                    break
            except (ConnectionError, OSError):
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("native loopback server unavailable")
            time.sleep(0.25)
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=[
                "--no-sandbox", "--no-proxy-server", f"--host-resolver-rules=MAP {SITE} 127.0.0.1"])
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            page.on("pageerror", lambda _: evidence.update(page_errors=evidence["page_errors"] + 1))
            def response_seen(response):
                from urllib.parse import urlsplit
                target = urlsplit(response.url)
                expected_csrf = stage == "native_csrf_denial" and target.path == "/api/method/" + PREFIX + "commitment_orders" and response.status == 400
                if target.hostname == SITE and target.path.startswith(("/api/", "/assets/")) and response.status >= 400 and not expected_csrf:
                    evidence["failed_native_requests"].append(dict(path=target.path[-200:], status=response.status))
                if target.path == "/api/method/" + PREFIX + "post_commitment":
                    evidence["posts"] += 1
                    evidence.setdefault("post_statuses", []).append(response.status)
            page.on("response", response_seen)
            def route_request(route):
                from urllib.parse import urlsplit
                target = urlsplit(route.request.url)
                if target.scheme in ("http", "https") and target.hostname != SITE:
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", route_request)
            try:
                stage = "native_ordinary_manager_login"
                page.goto(BASE + "/login?redirect-to=%2Fdesk%2Fhrp-budget-reservations", wait_until="domcontentloaded")
                page.locator("#login_email").fill(MANAGER)
                page.locator("#login_password").fill(os.environ["ADMIN_PASSWORD"])
                page.locator(".form-login:visible button[type='submit']").click()
                page.wait_for_function("name => !!(window.frappe?.boot?.user?.name === name && frappe.app && frappe.container && typeof frappe.set_route === 'function')", arg=MANAGER, timeout=90000)
                stage = "current_native_root_and_order"
                selection = page.evaluate("""async names => {
                    for (const name of names) {
                        const {message: state} = await frappe.call({method:'medops.api.v1.finance.reservation_operations.inspect',type:'POST',args:{payload:{reservation:name}}});
                        if (state.effect !== 'read_only' || !/[1-9]/.test(state.position.open_reservation)) continue;
                        const {message: list} = await frappe.call({method:'medops.api.v1.finance.reservation_operations.commitment_orders',type:'POST',args:{payload:{reservation:name,source_digest:state.source_digest}}});
                        if (list.orders.length === 1) return {root:name,source_digest:state.source_digest,order:list.orders[0],before:state.position};
                    }
                    throw new Error('CI linked current root absent');
                }""", roots)
                stage = "native_csrf_denial"
                denied = page.evaluate("""async query => {
                    const response = await fetch('/api/method/medops.api.v1.finance.reservation_operations.commitment_orders', {
                        method:'POST', headers:{'X-Frappe-CSRF-Token':'invalid-ci-token'},
                        body:new URLSearchParams({payload:JSON.stringify(query)})});
                    const data = await response.json();
                    return {status:response.status,exc_type:data.exc_type};
                }""", dict(reservation=selection["root"], source_digest=selection["source_digest"]))
                if denied["status"] != 400 or denied["exc_type"] != "CSRFTokenError":
                    raise AssertionError("native invalid CSRF token not rejected")
                evidence["native_csrf_denial"] = denied["status"]
                stage = "native_commitment_picker"
                page.evaluate("name => frappe.set_route('hrp-budget-reservations', name)", selection["root"])
                expect(page.get_by_text("预占编号：" + selection["root"], exact=True)).to_be_visible(timeout=180000)
                page.get_by_role("button", name="核对采购承诺", exact=True).click()
                picker = page.locator(".modal:visible").filter(has=page.get_by_text("选择采购订单并填写承诺金额", exact=True))
                expect(picker).to_be_visible(timeout=90000)
                row = selection["order"]
                label = f"{row['order']} · 明细行 {row['line_number']} · {row['item_code']} · {row['currency']} {row['amount']}"
                picker.get_by_role("button", name=label, exact=True).click()
                picker.locator(".frappe-control[data-fieldname='amount'] input").fill("1")
                picker.locator(".frappe-control[data-fieldname='reason'] textarea").fill("CI Chromium synthetic reviewed purchase commitment")
                picker.get_by_role("button", name="生成独立核对预览", exact=True).click()
                stage = "native_review_unchecked"
                review = page.locator(".modal:visible").filter(has=page.get_by_text("人工核对采购承诺", exact=True))
                expect(review).to_be_visible(timeout=180000)
                page.screenshot(path=str(output / "native-commitment-review-visible.png"), full_page=True)
                stage = "native_review_checkbox"
                checkbox = review.locator(".frappe-control[data-fieldname='acknowledge'] input[type='checkbox']")
                expect(checkbox).not_to_be_checked()
                stage = "native_review_actor"
                expect(review.get_by_text("执行人：" + MANAGER, exact=True)).to_be_visible()
                stage = "native_review_order"
                expect(review.get_by_text("采购订单：" + row["order"], exact=True)).to_be_visible()
                stage = "native_review_amount"
                expect(review.get_by_text("本次精确金额：" + row["currency"] + " " + format(1, f".{selection['before']['precision']}f"), exact=True)).to_be_visible()
                if evidence["posts"]:
                    raise AssertionError("post sent before acknowledgement")
                page.screenshot(path=str(output / "native-commitment-review.png"), full_page=True)
                stage = "native_unchecked_submit_denied"
                review.get_by_role("button", name="确认并转为采购承诺", exact=True).click()
                warning = page.locator(".modal:visible").filter(has=page.get_by_text("请核对后明确勾选人工确认。", exact=True))
                expect(warning).to_be_visible()
                if evidence["posts"]:
                    raise AssertionError("unchecked submit sent a post")
                warning.locator(".btn-modal-close").click()
                stage = "native_explicit_consent_post"
                checkbox.check()
                with page.expect_response(lambda response: response.url.endswith("/api/method/" + PREFIX + "post_commitment"), timeout=180000) as posted:
                    review.get_by_role("button", name="确认并转为采购承诺", exact=True).click()
                result = posted.value.json().get("message", {})
                if posted.value.status != 200 or result.get("effect") != "reservation_committed" or result.get("reservation") != selection["root"]:
                    raise AssertionError("native commitment not posted")
                expect(review).to_have_count(0, timeout=90000)
                stage = "native_reload_and_current_position"
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_text("预占编号：" + selection["root"], exact=True)).to_be_visible(timeout=180000)
                expect(page.locator(".modal:visible")).to_have_count(0)
                current = page.evaluate("""async name => (await frappe.call({method:'medops.api.v1.finance.reservation_operations.inspect',type:'POST',args:{payload:{reservation:name}}})).message.position""", selection["root"])
                from decimal import Decimal
                if Decimal(current["open_commitment"]) != Decimal(selection["before"]["open_commitment"]) + 1 or Decimal(current["open_reservation"]) != Decimal(selection["before"]["open_reservation"]) - 1:
                    raise AssertionError("native current amounts do not match one Commit")
                page.screenshot(path=str(output / "native-commitment-posted.png"), full_page=True)
                if evidence["posts"] != 1 or evidence.get("post_statuses") != [200] or evidence["page_errors"] or evidence["failed_native_requests"]:
                    raise AssertionError("native browser commands/errors invalid")
                stage = "native_logout"
                logged_out = page.evaluate("() => new Promise(resolve => frappe.call({method:'logout',type:'POST',callback:()=>resolve(true),error:()=>resolve(false)}))")
                if not logged_out:
                    raise AssertionError("native logout failed")
                context.close()
                browser.close()
            except BaseException as browser_error:
                # Capture while Chromium is still alive, before sync_playwright
                # unwinds. Never persist request bodies, cookies or proof values.
                evidence["browser_failed_stage"] = stage
                if stage.startswith("native_review") or stage.startswith("native_unchecked"):
                    evidence["safe_assertion_detail"] = str(browser_error)[:1800]
                try:
                    masks = [page.locator("#login_password")] if page.locator("#login_password").count() else []
                    page.screenshot(path=str(output / "failed-native-commitment.png"), full_page=False, mask=masks, timeout=10000)
                    evidence["browser_state"] = page.evaluate("() => ({path:location.pathname,has_boot:!!window.frappe?.boot,modals:document.querySelectorAll('.modal.show').length})")
                except BaseException as capture:
                    evidence["capture_error_type"] = type(capture).__name__
                raise
        stage = "immutable_rows_and_exact_append"
        frappe = connect(bench)
        after = snapshot(frappe)
        changed = {"HRP Budget Reservation Event": 1, "HRP Budget Commitment Authorization": 2}
        for table in TABLES:
            if any(after[table].get(name) != value for name, value in before[table].items()):
                raise AssertionError("preexisting financial/source row changed: " + table)
            if len(after[table]) - len(before[table]) != changed.get(table, 0):
                raise AssertionError("unexpected appended row count: " + table)
        events = set(after["HRP Budget Reservation Event"]) - set(before["HRP Budget Reservation Event"])
        event = after["HRP Budget Reservation Event"][events.pop()]
        if event.kind != "Commit" or event.reservation != selection["root"] or Decimal(str(event.amount)) != 1:
            raise AssertionError("unexpected committed event identity")
        error_logs = frappe.db.count("Error Log")
        if error_logs:
            raise AssertionError("native Error Log not empty")
        evidence.update(status="passed", error_log_count=error_logs, ordinary_manager_native_login=True,
                        unchecked_review=True, unchecked_submit_no_post=True, explicit_synthetic_consent=True,
                        reload_current_amounts=True, original_rows_immutable=True, exact_commit_append=True,
                        no_gl_sle_or_budget_release_change=True, before=safe_snapshot(frappe, before), after=safe_snapshot(frappe, after),
                        scope="Real Chromium/ordinary synthetic manager/native Desk picker/preview/unchecked rejection/explicit checkbox/post/reload/CSRF denial; no actual human UAT or hospital capacity claim")
        frappe.destroy()
        return 0
    except BaseException as error:
        evidence.update(failed_stage=stage, error_type=type(error).__name__,
                        primary_error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
        if page is not None:
            try:
                masks = [page.locator("#login_password")] if page.locator("#login_password").count() else []
                page.screenshot(path=str(output / "failed-native-commitment.png"), full_page=False, mask=masks, timeout=10000)
                evidence["browser_state"] = page.evaluate("() => ({path:location.pathname,has_boot:!!window.frappe?.boot,modals:document.querySelectorAll('.modal.show').length})")
            except BaseException as capture:
                evidence["capture_error_type"] = type(capture).__name__
        return 1
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        (output / "native-commitment-desk.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(dict(status=evidence["status"], stage=stage, scope="CI-only real Chromium commitment")))


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        raise SystemExit(main())
