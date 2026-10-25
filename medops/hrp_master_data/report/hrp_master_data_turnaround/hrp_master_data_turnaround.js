frappe.query_reports["HRP Master Data Turnaround"] = {
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
			fieldname: "from_date",
			label: "开始日期",
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: "结束日期",
			fieldtype: "Date",
		},
		{
			fieldname: "status",
			label: "状态",
			fieldtype: "Data",
		},
	],
};
