from medops.common.fixture_policy import get_frappe_fixture_hooks

app_name = "medops"
app_title = "医衡 MedOps"
app_publisher = "美妍伊人医疗科技有限公司"
app_description = "基于 Frappe 生态的医院资源规划平台"
app_email = "317395616@qq.com"
app_license = "License Review Required"
source_link = "https://github.com/jerry317395616/medops"

required_apps = ["jerry317395616/erpnext", "jerry317395616/hrms"]

app_logo_url = "/assets/medops/images/medops-logo.svg"
app_home = "/desk/hrp-core-workbench"
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": app_home,
		"has_permission": "medops.permissions.check_app_permission",
		"sequence_id": 3,
	}
]

before_install = "medops.setup.install.before_install"
after_install = "medops.setup.install.after_install"
after_migrate = "medops.setup.install.after_migrate"
before_uninstall = "medops.setup.install.before_uninstall"

before_request = ["medops.services.audit_context.start_http_audit_context"]
after_request = ["medops.services.audit_context.finish_http_audit_context"]
before_job = ["medops.services.audit_context.start_job_audit_context"]
after_job = ["medops.services.audit_context.finish_job_audit_context"]

# Add standard DocType extensions here. Prefer extend_doctype_class over overrides.
extend_doctype_class = {"Report": ["medops.extensions.core_report.CoreReportAccessMixin"]}
doc_events = {
	"User": {"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"},
	"Workflow": {
		"validate": "medops.hrp_budget.services.approval_native.protect_workflow",
		"on_trash": "medops.hrp_budget.services.approval_native.protect_workflow",
		"before_rename": "medops.hrp_budget.services.approval_native.protect_workflow",
	},
	"Workflow State": {
		"before_rename": "medops.hrp_budget.services.approval_native.protect_workflow_label",
		"on_trash": "medops.hrp_budget.services.approval_native.protect_workflow_label",
	},
	"Workflow Action Master": {
		"before_rename": "medops.hrp_budget.services.approval_native.protect_workflow_label",
		"on_trash": "medops.hrp_budget.services.approval_native.protect_workflow_label",
	},
	"Company": {"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"},
	"Currency": {"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"},
	"Fiscal Year": {
		"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"
	},
	"Cost Center": {
		"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"
	},
	"Account": {"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"},
	"Project": {"before_rename": "medops.hrp_budget.services.reference_integrity.protect_reference_rename"},
	"Report": {"validate": "medops.services.core_reports.protect_fresh_reports"},
	"HRP Data Quality Issue": {"on_update": "medops.hrp_master_data.services.quality_history.append_event"},
	"*": {
		"on_update": "medops.hrp_workflow_authorization.services.sla.sync_source",
		"on_update_after_submit": "medops.hrp_workflow_authorization.services.sla.sync_source",
		"on_cancel": "medops.hrp_workflow_authorization.services.sla.sync_source",
		"on_trash": "medops.hrp_workflow_authorization.services.sla.sync_source",
	},
	"ToDo": {
		"validate": [
			"medops.hrp_workflow_authorization.services.sla.protect_todo",
			"medops.hrp_budget.services.approval_native.protect_todo",
		],
		"on_trash": [
			"medops.hrp_workflow_authorization.services.sla.protect_todo",
			"medops.hrp_budget.services.approval_native.protect_todo",
		],
		"before_rename": [
			"medops.hrp_workflow_authorization.services.sla.protect_todo",
			"medops.hrp_budget.services.approval_native.protect_todo",
		],
	},
}

permission_query_conditions = {
	"HRP Budget Release": "medops.hrp_budget.services.release_access.release_query",
	"HRP Budget Release Entry": "medops.hrp_budget.services.release_access.event_query",
	"HRP Budget Ledger": "medops.hrp_budget.services.release_access.ledger_query",
	"HRP Integration Outbox": "medops.hrp_budget.services.release_access.outbox_query",
	"HRP Budget Approval": "medops.hrp_budget.services.approval_access.case_query",
	"HRP Budget Review": "medops.hrp_budget.services.approval_access.review_query",
	"ToDo": "medops.hrp_budget.services.approval_native.todo_query",
	"HRP Budget Submission": "medops.hrp_budget.services.submission_access.submission_query",
	"HRP Budget Submission Line": "medops.hrp_budget.services.submission_access.submission_line_query",
	"HRP Business Dimension": "medops.hrp_master_data.services.budget_dimensions.dimension_query",
	"HRP Dimension Value": "medops.hrp_master_data.services.budget_dimensions.value_query",
	"HRP Funding Source": "medops.hrp_master_data.services.budget_dimensions.fund_query",
	"HRP Budget Version": "medops.hrp_budget.services.planning_access.version_query",
	"HRP Budget Plan": "medops.hrp_budget.services.planning_access.plan_query",
	"HRP Budget Plan Line": "medops.hrp_budget.services.planning_access.line_query",
	"HRP Reference Link": "medops.hrp_foundation.services.reference_links.reference_query",
	"HRP Data Quality Event": "medops.services.core_report_access.history_query",
	"HRP Rule Execution Log": "medops.hrp_workflow_authorization.services.business_rules.log_query",
	"HRP Approval Matrix": "medops.hrp_workflow_authorization.permissions.approval_matrix_query",
	"HRP Delegation": "medops.hrp_workflow_authorization.permissions.delegation_query",
	"HRP Segregation Rule": "medops.hrp_workflow_authorization.permissions.segregation_rule_query",
	"HRP Hospital": "medops.hrp_workflow_authorization.permissions.hospital_query",
	"HRP Organization Mapping": "medops.hrp_workflow_authorization.permissions.organization_mapping_query",
	"HRP Organization Unit": "medops.hrp_workflow_authorization.permissions.organization_unit_query",
	"HRP Organization Version": "medops.hrp_workflow_authorization.permissions.organization_version_query",
	"HRP Data Quality Issue": "medops.hrp_master_data.permissions.data_quality_issue_query",
	"HRP Data Quality Rule": "medops.hrp_master_data.permissions.data_quality_rule_query",
	"HRP External Code Mapping": "medops.hrp_master_data.permissions.external_code_mapping_query",
	"HRP Master Data Request": "medops.hrp_master_data.permissions.master_data_request_query",
	"HRP Number Reservation": "medops.hrp_foundation.permissions.number_reservation_query",
	"HRP Numbering Scheme": "medops.hrp_foundation.permissions.numbering_scheme_query",
}
has_permission = {
	"HRP Budget Release": "medops.hrp_budget.services.release_access.release_permission",
	"HRP Budget Release Entry": "medops.hrp_budget.services.release_access.event_permission",
	"HRP Budget Ledger": "medops.hrp_budget.services.release_access.ledger_permission",
	"HRP Integration Outbox": "medops.hrp_budget.services.release_access.outbox_permission",
	"HRP Budget Approval": "medops.hrp_budget.services.approval_access.case_permission",
	"HRP Budget Review": "medops.hrp_budget.services.approval_access.review_permission",
	"ToDo": "medops.hrp_budget.services.approval_native.todo_permission",
	"HRP Budget Submission": "medops.hrp_budget.services.submission_access.submission_permission",
	"HRP Budget Submission Line": "medops.hrp_budget.services.submission_access.submission_line_permission",
	"HRP Business Dimension": "medops.hrp_master_data.services.budget_dimensions.master_permission",
	"HRP Dimension Value": "medops.hrp_master_data.services.budget_dimensions.master_permission",
	"HRP Funding Source": "medops.hrp_master_data.services.budget_dimensions.master_permission",
	"HRP Budget Version": "medops.hrp_budget.services.planning_access.version_permission",
	"HRP Budget Plan": "medops.hrp_budget.services.planning_access.plan_permission",
	"HRP Budget Plan Line": "medops.hrp_budget.services.planning_access.line_permission",
	"HRP Reference Link": "medops.hrp_foundation.services.reference_links.reference_permission",
	"HRP Data Quality Event": "medops.services.core_report_access.history_permission",
	"HRP Rule Execution Log": "medops.hrp_workflow_authorization.services.business_rules.log_permission",
	"HRP Approval Matrix": "medops.hrp_workflow_authorization.permissions.can_read_approval_matrix",
	"HRP Delegation": "medops.hrp_workflow_authorization.permissions.can_read_delegation",
	"HRP Segregation Rule": "medops.hrp_workflow_authorization.permissions.can_read_segregation_rule",
	"HRP Hospital": "medops.hrp_workflow_authorization.permissions.has_scoped_permission",
	"HRP Organization Mapping": "medops.hrp_workflow_authorization.permissions.has_scoped_permission",
	"HRP Organization Unit": "medops.hrp_workflow_authorization.permissions.has_scoped_permission",
	"HRP Organization Version": "medops.hrp_workflow_authorization.permissions.has_scoped_permission",
	"HRP Data Quality Issue": "medops.hrp_master_data.permissions.can_read_data_quality",
	"HRP Data Quality Rule": "medops.hrp_master_data.permissions.can_read_data_quality",
	"HRP External Code Mapping": "medops.hrp_master_data.permissions.can_read_external_code_mapping",
	"HRP Master Data Request": "medops.hrp_master_data.permissions.can_read_master_data_request",
	"HRP Number Reservation": "medops.hrp_foundation.permissions.can_read_number_reservation",
	"HRP Numbering Scheme": "medops.hrp_foundation.permissions.can_read_numbering_scheme",
}

scheduler_events = {
	"cron": {
		"* * * * *": [
			"medops.hrp_workflow_authorization.services.sla.run_due_sla_cases",
			"medops.hrp_budget.jobs.release.dispatch_due_releases",
		]
	},
	"daily": [
		"medops.hrp_master_data.services.data_quality.run_data_quality_rules",
		"medops.hrp_workflow_authorization.services.delegation.sync_delegation_statuses",
		"medops.setup.maintenance.daily_maintenance",
	],
}

fixture_auto_order = True
fixtures = get_frappe_fixture_hooks()

export_python_type_annotations = True
require_type_annotated_api_methods = True

override_whitelisted_methods = {
	"frappe.desk.query_report.export_query": "medops.services.core_reports.export_query",
	"frappe.desk.search.get_link_title": "medops.hrp_budget.services.link_titles.get_link_title",
}
