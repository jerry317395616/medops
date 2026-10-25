frappe.query_reports["HRP Data Quality Issue Trend"] = {
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
			fieldname: "master_data_domain",
			label: "主数据域",
			fieldtype: "Link",
			options: "HRP Master Data Domain",
		},
		{
			fieldname: "from_date",
			label: "开始日期",
			fieldtype: "Date",
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: "结束日期",
			fieldtype: "Date",
			reqd: 1,
		},
	],
};
