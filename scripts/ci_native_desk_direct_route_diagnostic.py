"""CI-only login/empty Desk diagnosis; no financial facts or credentials in output."""

import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ci_native_reservation_desk_probe import BASE, PORT, SITE, connect, snapshot


def main():
    bench = Path(os.environ["BENCH_DIR"]).resolve()
    process = browser = context = page = None
    stage = "containment"
    result = {"status": "failed", "page_errors": [], "failed_assets": [], "financial_calls": 0,
              "network": []}
    output = Path(os.environ["RUNNER_TEMP"]) / "native-reservation-desk-evidence"
    try:
        frappe = connect(bench)
        from medops.tests.disposable_reservation_site import require_disposable_reservation_site
        from medops.tests.test_budget_consolidation import fixtures
        require_disposable_reservation_site(empty=True)
        output.mkdir()
        fixtures()
        frappe.get_single("Installed Applications").update_versions()
        frappe.db.commit()
        result["setup_complete"] = frappe.is_setup_complete()
        result["default_home_is_setup_wizard"] = frappe.db.get_default("desktop:home_page") == "setup-wizard"
        before = snapshot(frappe)
        frappe.destroy()
        server_script = Path(__file__).with_name("ci_native_reservation_desk_probe.py")
        with (bench / "logs" / "native-desk-private-server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, str(server_script), "--serve"],
                                       cwd=bench / "sites", stdout=log, stderr=log)
        deadline = time.monotonic() + 60
        stage = "loopback_server"
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
            except (OSError, ConnectionError):
                pass
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("owned loopback server unavailable")
            time.sleep(0.25)

        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=[
                "--no-sandbox", "--no-proxy-server", f"--host-resolver-rules=MAP {SITE} 127.0.0.1",
            ])
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            page = context.new_page()

            def page_error(error):
                message = str(error.message)
                safe = {"message_sha256": hashlib.sha256(message.encode()).hexdigest(), "static_frames": []}
                for pattern, kind in (
                    (r"(?:ReferenceError: )?([A-Za-z_$][\w$]*) is not defined", "undefined_js_symbol"),
                    (r"Cannot read properties of (?:undefined|null) \(reading '([A-Za-z_$][\w$]*)'\)", "undefined_property"),
                ):
                    match = re.search(pattern, message)
                    if match:
                        safe.update(kind=kind, symbol=match[1])
                        break
                for frame in re.findall(r"https?://[^\s)]+", str(error.stack or "")):
                    path = urlsplit(frame).path
                    if path.startswith("/assets/"):
                        safe["static_frames"].append(path[-250:])
                result["page_errors"].append(safe)

            page.on("pageerror", page_error)

            def response_seen(response):
                target = urlsplit(response.url)
                if target.hostname == SITE and (target.path.startswith("/api/") or target.path.startswith("/assets/")):
                    event = {"path": target.path[-200:], "status": response.status}
                    if target.path.endswith("/frappe.desk.desk_page.getpage"):
                        name = parse_qs(urlsplit(response.request.url).query).get("name", [""])[0]
                        if not name and response.request.post_data:
                            name = parse_qs(response.request.post_data).get("name", [""])[0]
                        event["known_page"] = name if name in {"setup-wizard", "desktop", "workspace", "app", "hrp-budget-reservations"} else "other"
                    result["network"].append(event)
                    result["network"] = result["network"][-50:]
                if response.status >= 400 and target.path.startswith("/assets/"):
                    result["failed_assets"].append({"path": target.path[-250:], "status": response.status})

            page.on("response", response_seen)

            def route_request(route):
                request = route.request
                target = urlsplit(request.url)
                if target.scheme in ("http", "https") and target.hostname != SITE:
                    route.abort()
                elif "medops.api.v1.finance." in request.url:
                    result["financial_calls"] += 1
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", route_request)
            try:
                stage = "login_navigation"
                page.goto(BASE + "/login?redirect-to=%2Fdesk%2Fhrp-budget-reservations", wait_until="domcontentloaded")
                stage = "login_email_control"
                page.locator("#login_email").fill("Administrator")
                stage = "login_password_control"
                page.locator("#login_password").fill(os.environ["ADMIN_PASSWORD"])
                stage = "login_submit_control"
                page.locator(".form-login:visible button[type='submit']").click()
                stage = "native_boot"
                page.wait_for_function("window.frappe && frappe.boot && frappe.boot.user && frappe.boot.user.name === 'Administrator' && frappe.app && frappe.container && frappe.set_route", timeout=90000)
                stage = "empty_reservation_page"
                page.evaluate("() => frappe.set_route('hrp-budget-reservations')")
                expect(page.get_by_text("采购预算预占", exact=True).first).to_be_visible(timeout=60000)
                expect(page.get_by_role("button", name="选择采购明细", exact=True)).to_be_visible()
                page.screenshot(path=str(output / "empty-native-desk.png"), full_page=True)
                stage = "native_logout"
                logged_out = page.evaluate("() => new Promise(resolve => frappe.call({method: 'logout', type: 'POST', callback: () => resolve(true), error: () => resolve(false)}))")
                if not logged_out:
                    raise AssertionError("native logout failed")
                context.close()
                browser.close()
                page = context = browser = None
            except BaseException as primary:
                result["primary_error_type"] = type(primary).__name__
                result["primary_error_sha256"] = hashlib.sha256(str(primary).encode()).hexdigest()
                result["browser_state"] = page.evaluate("() => ({path: location.pathname, ready: document.readyState, has_frappe: !!window.frappe, has_boot: !!window.frappe?.boot, admin_boot: window.frappe?.boot?.user?.name === 'Administrator', boot_setup_complete: !!window.frappe?.boot?.setup_complete, boot_default_setup_complete: !!window.frappe?.boot?.sysdefaults?.setup_complete, boot_home_is_setup_wizard: window.frappe?.boot?.home_page === "setup-wizard", has_jquery: !!window.jQuery, has_app: !!window.frappe?.app, has_application_class: !!window.frappe?.Application, has_container: !!window.frappe?.container, has_route: !!window.frappe?.set_route, body_children: document.querySelector('#body')?.childElementCount ?? -1, script_count: document.scripts.length, email_visible: !!document.querySelector('#login_email')?.offsetParent, password_visible: !!document.querySelector('#login_password')?.offsetParent})")
                try:
                    masks = [page.locator("#login_password")] if page.locator("#login_password").count() else []
                    page.screenshot(path=str(output / "failed-native-desk.png"), full_page=False,
                                    mask=masks, timeout=10000)
                except BaseException as capture:
                    result["capture_error_type"] = type(capture).__name__
                context.unroute_all(behavior="ignoreErrors")
                raise

        stage = "final_evidence"
        frappe = connect(bench)
        after = snapshot(frappe)
        errors = frappe.db.count("Error Log")
        frappe.destroy()
        if before != after or errors or result["financial_calls"] or result["page_errors"]:
            raise AssertionError("native observation did not preserve evidence")
        result.update(status="passed", immutable_snapshots_identical=True, error_log_count=errors,
                      scope="Plain Chromium/native password login with explicit native Page redirect and empty MedOps Page only; no valid-root UI, finance, recovery controls, Core browser or hospital acceptance.")
        return 0
    except BaseException as error:
        result.update(failed_stage=stage, error_type=type(error).__name__)
        return 1
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if output.is_dir():
            (output / "native-desk-login.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"status": result["status"], "stage": stage, "scope": "CI-only native login diagnostic"}))


if __name__ == "__main__":
    raise SystemExit(main())
