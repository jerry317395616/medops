from __future__ import annotations

import json

import click

from medops.scaffold.module import DOMAIN_GROUPS, create_module_files


@click.command("medops-create-module")
@click.option("--name", required=True, help='Display name, for example "HRP Medical Insurance".')
@click.option("--group", "domain_group", required=True, type=click.Choice(DOMAIN_GROUPS, case_sensitive=True))
@click.option("--label-cn", required=True, help="Chinese module label.")
@click.option("--description", required=True, help="One-line module responsibility description.")
@click.option("--yes", is_flag=True, help="Confirm that source files will be modified.")
def create_module_command(
	name: str,
	domain_group: str,
	label_cn: str,
	description: str,
	yes: bool,
) -> None:
	"""Create a version-controlled module inside the single medops app."""
	if not yes:
		raise click.ClickException(
			"This command modifies app source. Review the branch and rerun with --yes."
		)
	try:
		result = create_module_files(
			name=name,
			domain_group=domain_group,
			label_cn=label_cn,
			description=description,
		)
	except (ValueError, FileExistsError, FileNotFoundError) as exc:
		raise click.ClickException(str(exc)) from exc
	click.echo(json.dumps(result, ensure_ascii=False, indent=2))
	click.echo("Review and commit the files, then run: bench --site <site> migrate")


commands = [create_module_command]
