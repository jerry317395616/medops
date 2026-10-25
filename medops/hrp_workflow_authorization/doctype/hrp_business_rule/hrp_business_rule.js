frappe.ui.form.on("HRP Business Rule", {
	refresh(frm) {
		frm.disable_save();
		if (!["System Manager", "HRP System Manager"].some((r) => frappe.user_roles.includes(r))) return;
		frm.add_custom_button(__("保存业务规则"), async () => {
			const wasNew = frm.is_new();
			const fields = [
				"code",
				"display_name",
				"target_doctype",
				"target_action",
				"company",
				"hospital",
				"organization_unit",
				"include_descendants",
				"enabled",
				"priority",
				"valid_from",
				"valid_to",
				"severity",
				"when_json",
				"assertion_json",
				"remarks",
			];
			const policy = Object.fromEntries(fields.map((f) => [f, frm.doc[f] ?? null]));
			const response = await frappe.call({
				method: "medops.api.v1.core.rules.upsert",
				args: { policy, expected_revision: wasNew ? 0 : frm.doc.revision },
				headers: { "Idempotency-Key": `business-rule-${frappe.utils.get_random(16)}` },
				freeze: true,
			});
			frm.doc.__unsaved = 0;
			if (wasNew) frappe.set_route("Form", "HRP Business Rule", response.message.name);
			else await frm.reload_doc();
		});
	},
});
