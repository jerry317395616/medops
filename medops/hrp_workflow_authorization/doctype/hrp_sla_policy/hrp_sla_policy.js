frappe.ui.form.on("HRP SLA Policy", {
	refresh(frm) {
		frm.disable_save();
		if (!["System Manager", "HRP System Manager"].some((r) => frappe.user_roles.includes(r))) return;
		frm.add_custom_button(__("保存服务时限策略"), async () => {
			const fields = [
				"code",
				"display_name",
				"target_doctype",
				"company",
				"hospital",
				"organization_unit",
				"include_descendants",
				"priority",
				"enabled",
				"valid_from",
				"valid_to",
				"status_field",
				"start_field",
				"response_field",
				"start_statuses_json",
				"pause_statuses_json",
				"completion_statuses_json",
				"response_seconds",
				"resolution_seconds",
				"clock_basis",
				"support_hours_json",
				"holiday_list",
				"responsible_user",
				"escalation_steps_json",
				"remarks",
			];
			const policy = Object.fromEntries(fields.map((f) => [f, frm.doc[f] ?? null]));
			const response = await frappe.call({
				method: "medops.api.v1.core.sla.upsert_policy",
				args: { policy, expected_revision: frm.is_new() ? 0 : frm.doc.revision },
				headers: { "Idempotency-Key": `sla-policy-${frappe.utils.get_random(16)}` },
				freeze: true,
			});
			await frappe.set_route("Form", "HRP SLA Policy", response.message.name);
			await frm.reload_doc();
		});
	},
});
