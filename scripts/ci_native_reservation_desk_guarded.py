"""CI-only real Chromium/Desk observation; never run on an existing deployment."""

import hashlib
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SITE = "hrp-reservation-concurrency.localhost"
PORT = 18137
BASE = f"http://{SITE}:{PORT}"
TABLES = (
    "HRP Budget Reservation", "HRP Budget Reservation Event",
    "HRP Budget Reservation Authorization", "HRP Budget Reservation Renewal",
    "HRP Budget Reservation Release", "GL Entry", "Stock Ledger Entry",
    "HRP Budget Ledger",
)


def connect(bench):
    import frappe
    from medops.tests.disposable_reservation_site import require_disposable_reservation_site

    frappe.init(site=SITE, sites_path=str(bench / "sites"))
    frappe.connect()
    require_disposable_reservation_site()
    frappe.set_user("Administrator")
    return frappe


def snapshot(frappe):
    result = {}
    for table in TABLES:
        rows = frappe.get_all(table, fields=["*"], order_by="name", limit_page_length=0)
        result[table] = {
            "count": len(rows),
            "sha256": hashlib.sha256(frappe.as_json(rows).encode()).hexdigest(),
        }
    return result


def serve():
    bench = Path(os.environ["BENCH_DIR"]).resolve()
    if Path.cwd().resolve() != bench / "sites":
        raise RuntimeError("disposable sites directory required")
    checked = connect(bench)
    checked.destroy()
    import frappe.app
    from werkzeug.serving import run_simple

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    run_simple("127.0.0.1", PORT, frappe.app.application_with_statics(),
               threaded=True, use_reloader=False, use_debugger=False)


def main():
    bench = Path(os.environ["BENCH_DIR"]).resolve()
    output = Path(os.environ["RUNNER_TEMP"]) / "native-reservation-desk-evidence"
    output.mkdir()
    stage, process = "containment", None
    evidence = {"status": "failed", "financial_commands": 0, "inspection_statuses": [], "recovery_statuses": [],
                "page_errors": 0, "failed_native_requests": [], "server_touched": "disposable GitHub CI only", "include_core": os.environ.get("INCLUDE_IONE_CORE", "0"), "product_source": "524557a20a8ffaf41e9fc25ccb64e8fd9c7a2dc2"}
    try:
        frappe = connect(bench)
        frappe.get_single("Installed Applications").update_versions()
        from frappe.desk.page.setup_wizard.setup_wizard import disable_future_access
        disable_future_access()
        frappe.clear_cache()
        frappe.db.commit()
        evidence["native_setup_info_refreshed"] = True
        evidence["native_setup_complete"] = frappe.is_setup_complete()
        before = snapshot(frappe)
        roots = frappe.get_all("HRP Budget Reservation", pluck="name", order_by="creation asc", limit_page_length=11)
        if not 1 <= len(roots) <= 10:
            raise AssertionError("bounded completed committed probe required")
        frappe.destroy()
        stage = "loopback_wsgi_start"
        with (bench / "logs" / "native-desk-private-server.log").open("w") as private_log:
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
                raise AssertionError("loopback server unavailable")
            time.sleep(0.25)

        from playwright.sync_api import expect, sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=[
                "--no-sandbox", "--no-proxy-server",
                f"--host-resolver-rules=MAP {SITE} 127.0.0.1",
            ])
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()
            def safe_page_error(error):
                message = str(error.message)
                details = {"message_sha256": hashlib.sha256(message.encode()).hexdigest()}
                match = re.search(r"([A-Za-z_$][\w$]*) is not defined", message)
                if match:
                    details.update(kind="undefined_js_symbol", symbol=match[1])
                evidence["page_errors"] += 1
                evidence.setdefault("safe_page_errors", []).append(details)
            page.on("pageerror", safe_page_error)

            def response_seen(response):
                from urllib.parse import urlsplit
                target = urlsplit(response.url)
                if "medops.api.v1.finance.reservation_operations.inspect" in response.url:
                    evidence["inspection_statuses"].append(response.status)
                if "medops.api.v1.finance.reservation_operations.recovery" in response.url:
                    evidence["recovery_statuses"].append(response.status)
                if target.hostname == SITE and target.path.startswith(("/api/", "/assets/")) and response.status >= 400:
                    evidence["failed_native_requests"].append({"path": target.path[-200:], "status": response.status})

            page.on("response", response_seen)

            def route_request(route):
                from urllib.parse import urlsplit
                request = route.request
                target = urlsplit(request.url)
                if target.scheme in ("http", "https") and target.hostname != SITE:
                    route.abort()
                elif target.path.startswith("/api/method/medops.api.v1.finance.") and target.path.removeprefix("/api/method/") not in {
                    "medops.api.v1.finance.reservation_operations.inspect",
                    "medops.api.v1.finance.reservation_operations.recovery",
                    "medops.api.v1.finance.reservation.source",
                    "medops.api.v1.finance.ledger.statement",
                }:
                    evidence["financial_commands"] += 1
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", route_request)
            try:
                stage = "native_login"
                page.goto(BASE + "/login?redirect-to=%2Fdesk%2Fhrp-budget-reservations", wait_until="domcontentloaded")
                page.locator("#login_email").fill("Administrator")
                page.locator("#login_password").fill(os.environ["ADMIN_PASSWORD"])
                page.locator(".form-login:visible button[type='submit']").click()
                page.wait_for_function("window.frappe && frappe.boot && frappe.boot.user && frappe.boot.user.name === 'Administrator' && frappe.app && frappe.container && typeof frappe.set_route === 'function'", timeout=90000)
                stage = "select_authorized_open_root"
                root = page.evaluate("""async names => {
                    for (const name of names) {
                        const item = await new Promise((resolve,reject) => frappe.call({method:'medops.api.v1.finance.reservation_operations.inspect',type:'POST',args:{payload:{reservation:name}},callback:r=>resolve(r.message),error:()=>reject(new Error('CI inspection failed'))}));
                        if (item.effect !== 'read_only' || item.position?.reservation !== name) throw new Error('CI inspection response invalid');
                        if (/[1-9]/.test(item.position.open_reservation)) return name;
                    }
                    throw new Error('CI open reservation absent');
                }""", roots)
                stage = "native_reservation_route"
                page.evaluate("name => frappe.set_route('hrp-budget-reservations', name)", root)
                page.wait_for_url(BASE + "/desk/hrp-budget-reservations/" + root)
                record = page.get_by_text("预占编号：" + root, exact=True)
                expect(record).to_be_visible(timeout=180000)
                expect(page.get_by_role("button", name="核对并释放", exact=True)).to_be_visible()
                expect(page.locator(".modal:visible")).to_have_count(0)
                stage = "native_reload"
                page.reload(wait_until="domcontentloaded")
                expect(record).to_be_visible(timeout=180000)
                expect(page.locator(".modal:visible")).to_have_count(0)
                stage = "native_release_input_dialog"
                page.get_by_role("button", name="核对并释放", exact=True).click()
                expect(page.get_by_role("dialog")).to_be_visible()
                expect(page.get_by_role("button", name="生成独立核对预览", exact=True)).to_be_visible()
                page.screenshot(path=str(output / "native-reservation-desk.png"), full_page=True)
                if evidence["financial_commands"] or evidence["page_errors"] or evidence["failed_native_requests"] or len(evidence["inspection_statuses"]) < 3:
                    raise AssertionError("unexpected browser commands/errors or missing reload inspection")
                if any(status != 200 for status in evidence["inspection_statuses"]):
                    raise AssertionError("browser inspection failed")
                stage = "native_recovery_fresh_page"
                page.locator(".modal:visible .btn-modal-close").click()
                expect(page.locator(".modal:visible")).to_have_count(0)
                # Fresh navigation destroys the old page closure/journal. Obtain
                # only current authorized facts using the native cookie API.
                page.goto(BASE + "/desk/hrp-budget-reservations", wait_until="domcontentloaded")
                page.wait_for_function("window.frappe && frappe.boot && frappe.boot.user && frappe.boot.user.name === 'Administrator' && frappe.app && frappe.container && typeof frappe.set_route === 'function'", timeout=90000)
                choose = page.get_by_role("button", name="选择采购明细", exact=True)
                expect(choose).to_be_enabled(timeout=90000)
                details = page.evaluate("""async name => {
                    const r = await frappe.call({method:'medops.api.v1.finance.reservation_operations.inspect',type:'POST',args:{payload:{reservation:name}}});
                    if (r.message?.effect !== 'read_only' || r.message?.position?.reservation !== name) throw new Error('Fresh recovery context invalid');
                    return {source:r.message.source,key:r.message.position.key,allocation:r.message.allocation,currency:r.message.position.currency};
                }""", root)
                for field, value in (("request",details["source"]["request"]),("organization_unit",details["key"]["organization_unit"]),("fiscal_year",details["key"]["fiscal_year"])):
                    control = page.locator(f".frappe-control[data-fieldname='{field}']:visible input")
                    control.fill(value)
                    control.press("Tab")
                expect(choose).to_be_enabled(timeout=30000)
                with page.expect_response(lambda r: "/api/method/frappe.client.get" in r.url and r.status == 200, timeout=90000) as source_response:
                    choose.click()
                doc = source_response.value.json()["message"]
                rows = [r for r in doc["items"] if r["name"] == details["source"]["line"]]
                if len(rows) != 1:
                    raise AssertionError("Recovery native source row absent")
                label = str(rows[0]["idx"]) + " · " + rows[0]["item_code"]
                page.get_by_role("button", name=label, exact=True).click()
                expect(page.get_by_text("原单精确金额：" + details["currency"] + " " + details["source"]["amount"], exact=True)).to_be_visible(timeout=90000)
                stage = "native_recovery_budget_picker"
                with page.expect_response(lambda r: "/api/method/medops.api.v1.finance.ledger.statement" in r.url and r.status == 200, timeout=90000) as ledger_response:
                    page.get_by_role("button", name="选择下达预算", exact=True).click()
                book = ledger_response.value.json()["message"]
                candidates = [(i,row) for i,row in enumerate(book["rows"]) if not row.get("is_reversal") and all(row.get(k)==v for k,v in details["key"].items()) and re.search(r"[1-9]",row["amount"])]
                if not candidates:
                    raise AssertionError("Recovery budget absent from native authorized picker")
                index, row = candidates[0]
                labels = (book.get("display_labels") or [{} for _ in book["rows"]])[index]
                label = f"{labels.get('budget_subject') or row['budget_subject']} · {labels.get('funding_source') or row['funding_source']} · {row['period_start']} — {row['period_end']}"
                stage = "native_recovery_directory"
                with page.expect_response(lambda r: "/api/method/medops.api.v1.finance.reservation_operations.recovery" in r.url and r.status == 200, timeout=180000) as directory_response:
                    page.get_by_role("button", name=label, exact=True).first.click()
                directory = directory_response.value.json()["message"]
                matches = [r for r in directory["rows"] if r.get("reservation")==root and r.get("allocation")==details["allocation"] and r.get("state")=="Posted"]
                if len(matches)!=1 or directory.get("effect")!="read_only":
                    raise AssertionError("Recovery directory failed current-root identity")
                card = page.locator(".border.rounded.p-3").filter(has=page.get_by_text("分摊编号：" + details["allocation"],exact=True))
                expect(card.get_by_text("当前状态：已发生预占",exact=True)).to_be_visible(timeout=90000)
                page.screenshot(path=str(output / "native-recovery-directory.png"),full_page=True)
                stage = "native_recovery_repeat_query"
                with page.expect_response(lambda r: "/api/method/medops.api.v1.finance.reservation_operations.recovery" in r.url and r.status==200,timeout=180000):
                    page.get_by_role("button",name="查询历史决定与结果",exact=True).click()
                expect(card.get_by_text("当前状态：已发生预占",exact=True)).to_be_visible(timeout=90000)
                card.get_by_role("button",name="查看已发生预占",exact=True).click()
                expect(page.get_by_text("预占编号："+root,exact=True)).to_be_visible(timeout=180000)
                expect(page.locator(".modal:visible")).to_have_count(0)
                if evidence["financial_commands"] or evidence["page_errors"] or evidence["failed_native_requests"] or len(evidence["recovery_statuses"])<2 or any(status!=200 for status in evidence["recovery_statuses"]):
                    raise AssertionError("Recovery directory browser commands/errors/statuses invalid")
                evidence.update(fresh_page_recovery_directory=True,repeated_recovery_query=True,recovered_posted_root=True,recovery_screenshot="native-recovery-directory.png")
                stage = "native_logout"
                logged_out = page.evaluate("() => new Promise(resolve => frappe.call({method: 'logout', type: 'POST', callback: () => resolve(true), error: () => resolve(false)}))")
                if not logged_out:
                    raise AssertionError("logout failed")
                context.close()
                browser.close()
            except BaseException as primary:
                evidence["primary_error_type"] = type(primary).__name__
                evidence["primary_error_sha256"] = hashlib.sha256(str(primary).encode()).hexdigest()
                try:
                    evidence["browser_state"] = page.evaluate("() => ({path: location.pathname, ready: document.readyState, has_boot: !!window.frappe?.boot, has_boot_user: !!window.frappe?.boot?.user, admin_boot: window.frappe?.boot?.user?.name === 'Administrator', has_jquery: !!window.jQuery, has_app: !!window.frappe?.app, has_container: !!window.frappe?.container, has_route: typeof window.frappe?.set_route === 'function', modal_count: document.querySelectorAll('.modal.show').length})")
                    masks = [page.locator("#login_password")] if page.locator("#login_password").count() else []
                    page.screenshot(path=str(output / "failed-native-root-desk.png"), full_page=False, mask=masks, timeout=10000)
                except BaseException as capture:
                    evidence["capture_error_type"] = type(capture).__name__
                context.unroute_all(behavior="ignoreErrors")
                raise

        stage = "final_immutable_evidence"
        frappe = connect(bench)
        after = snapshot(frappe)
        error_logs = frappe.db.count("Error Log")
        frappe.destroy()
        if before != after or error_logs:
            raise AssertionError("financial evidence changed or Error Log appeared")
        evidence.update(status="passed", finance_before_after_identical=True, error_log_count=error_logs,
                        snapshot=before, authenticated_native_login=True, native_reload=True,
                        release_input_dialog=True, native_logout=True, screenshot="native-reservation-desk.png",
                        scope="Real native Frappe Desk/Chromium render, hashed bundle and read-only root inspection/reload; no preview/confirmation/financial command. Posted recovery directory controls tested after fresh navigation. Expired-decision renewal controls, non-admin UI, human UAT and hospital capacity not established.")
        return 0
    except BaseException as error:
        evidence.update(failed_stage=stage, error_type=type(error).__name__, primary_error_sha256=hashlib.sha256(str(error).encode()).hexdigest())
        if 'page' in locals():
            try:
                masks = [page.locator("#login_password")] if page.locator("#login_password").count() else []
                page.screenshot(path=str(output / "failed-native-root-desk.png"), full_page=False, mask=masks, timeout=10000)
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
        (output / "native-reservation-desk.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps({"status": evidence["status"], "stage": stage, "scope": "CI-only native Desk"}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--serve"]:
        serve()
    else:
        raise SystemExit(main())
