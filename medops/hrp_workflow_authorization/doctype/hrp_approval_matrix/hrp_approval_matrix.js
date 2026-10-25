frappe.ui.form.on("HRP Approval Matrix", {
	refresh(frm) {
		const exact = Number(frm.doc.amount_schema_version || 1) === 2;
		frm.fields_dict.steps.grid.toggle_display("threshold_amount", !exact);
		frm.fields_dict.steps.grid.toggle_display("exact_threshold_amount", exact);
	},
});
