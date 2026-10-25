frappe.pages["hrp-sla-console"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("服务时限与超时待办"),
		single_column: true,
	});
	const body = $("<div>").appendTo(page.body);
	let offset = 0;
	const load = async () => {
		const { message } = await frappe.call({
			method: "medops.api.v1.core.sla.list_my",
			args: { offset, limit: 20 },
		});
		body.empty();
		$("<p>")
			.text(
				__(
					"仅显示您有权读取的业务单据。确认响应不会批准或办结业务。时间按站点时区显示。要点开单据处理业务。",
				),
			)
			.appendTo(body);
		if (!message.items.length) $("<p>").text(__("本页没有可见的计时记录。")).appendTo(body);
		for (const item of message.items) {
			const row = $("<div class='border rounded p-3 mb-3'>").appendTo(body);
			$("<a>")
				.text(`${item.reference_doctype} / ${item.reference_name}`)
				.attr("href", frappe.utils.get_form_link(item.reference_doctype, item.reference_name))
				.appendTo(row);
			$("<p>")
				.text(
					`${__(item.status)} · ${__("响应截止")}: ${item.response_due_at || "—"} · ${__("处理截止")}: ${item.resolution_due_at || "—"}`,
				)
				.appendTo(row);
			if (item.last_error_code)
				$("<p class='text-danger'>")
					.text(`${__("计时检查待重试")}: ${item.last_error_code}`)
					.appendTo(row);
			if (item.status === "Open" && !item.responded_at) {
				$("<button class='btn btn-sm btn-primary'>")
					.text(__("确认响应"))
					.appendTo(row)
					.on("click", async (event) => {
						$(event.currentTarget).prop("disabled", true);
						try {
							await frappe.call({
								method: "medops.api.v1.core.sla.act",
								args: { case_name: item.name, action: "acknowledge", expected_revision: item.revision },
								headers: { "Idempotency-Key": `sla-ack-${frappe.utils.get_random(16)}` },
							});
							await load();
						} finally {
							$(event.currentTarget).prop("disabled", false);
						}
					});
			}
		}
		if (message.next_offset != null)
			$("<button class='btn btn-default'>")
				.text(__("下一页"))
				.appendTo(body)
				.on("click", () => {
					offset = message.next_offset;
					return load();
				});
	};
	page.set_primary_action(__("刷新"), () => {
		offset = 0;
		return load();
	});
	load();
};
