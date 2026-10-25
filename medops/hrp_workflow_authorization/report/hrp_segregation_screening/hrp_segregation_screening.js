frappe.query_reports["HRP Segregation Screening"] = {
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
			fieldname: "subject_user",
			label: "被检查用户",
			fieldtype: "Link",
			options: "User",
			reqd: 1,
		},
		{
			fieldname: "action",
			label: "动作",
			fieldtype: "Select",
			options: "create\nsubmit\napprove\npost\npay\nreview",
			reqd: 1,
		},
		{
			fieldname: "target_doctype",
			label: "单据类型",
			fieldtype: "Link",
			options: "DocType",
			reqd: 1,
		},
		{
			fieldname: "docname",
			label: "单据编号",
			fieldtype: "Dynamic Link",
			options: "target_doctype",
			reqd: 1,
		},
	],
};
