frappe.query_reports["HRP Organization Version Difference"] = {
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
			fieldname: "from_version",
			label: "原已发布版本",
			fieldtype: "Link",
			options: "HRP Organization Version",
			reqd: 1,
		},
		{
			fieldname: "to_version",
			label: "新已发布版本",
			fieldtype: "Link",
			options: "HRP Organization Version",
			reqd: 1,
		},
	],
};
