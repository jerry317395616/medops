frappe.query_reports["HRP Delegation and Expiry Register"] = {
	filters: [
		{
			fieldname: "company",
			label: "法人",
			fieldtype: "Link",
			options: "Company",
		},
		{
			fieldname: "hospital",
			label: "医院",
			fieldtype: "Link",
			options: "HRP Hospital",
		},
		{
			fieldname: "organization_unit",
			label: "科室或组织",
			fieldtype: "Link",
			options: "HRP Organization Unit",
		},
		{
			fieldname: "status",
			label: "状态",
			fieldtype: "Data",
		},
		{
			fieldname: "matrix",
			label: "审批矩阵",
			fieldtype: "Link",
			options: "HRP Approval Matrix",
		},
		{
			fieldname: "from_user",
			label: "委托人",
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "to_user",
			label: "受托人",
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "from_date",
			label: "开始日期",
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: "结束日期",
			fieldtype: "Date",
		},
	],
};
