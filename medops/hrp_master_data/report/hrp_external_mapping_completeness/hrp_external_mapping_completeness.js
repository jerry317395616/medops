frappe.query_reports["HRP External Mapping Completeness"] = {
	filters: [
		{
			fieldname: "company",
			label: "法人",
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
		},
		{
			fieldname: "hospital",
			label: "医院",
			fieldtype: "Link",
			options: "HRP Hospital",
			reqd: 1,
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
			reqd: 1,
		},
		{
			fieldname: "as_of",
			label: "业务日期",
			fieldtype: "Date",
		},
		{
			fieldname: "external_system",
			label: "外部系统",
			fieldtype: "Data",
			reqd: 1,
		},
	],
};
