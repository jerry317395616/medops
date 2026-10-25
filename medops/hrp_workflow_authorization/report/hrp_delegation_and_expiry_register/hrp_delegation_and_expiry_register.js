frappe.query_reports["HRP Delegation and Expiry Register"] = {
	filters: [
		{
			fieldname: "status",
			label: __("状态"),
			fieldtype: "Select",
			options: "\nScheduled\nActive\nRevoked\nExpired",
		},
		{ fieldname: "matrix", label: __("审批矩阵"), fieldtype: "Link", options: "HRP Approval Matrix" },
		{ fieldname: "from_user", label: __("委托人"), fieldtype: "Link", options: "User" },
		{ fieldname: "to_user", label: __("受托人"), fieldtype: "Link", options: "User" },
	],
};
