from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
	sys.path.insert(0, str(REPOSITORY_ROOT))

from medops.scaffold.module import DOMAIN_GROUPS, create_module_files


def main() -> None:
	parser = argparse.ArgumentParser(description="Create a new module inside the single medops app")
	parser.add_argument("--name", required=True, help='Display name, e.g. "HRP Medical Insurance"')
	parser.add_argument("--group", required=True, choices=DOMAIN_GROUPS)
	parser.add_argument("--label-cn", required=True)
	parser.add_argument("--description", required=True)
	parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
	args = parser.parse_args()

	try:
		result = create_module_files(
			name=args.name,
			domain_group=args.group,
			label_cn=args.label_cn,
			description=args.description,
			app_root=args.root,
		)
	except (ValueError, FileExistsError, FileNotFoundError) as exc:
		parser.error(str(exc))

	print(json.dumps(result, ensure_ascii=False, indent=2))
	print("Next: review files, add DocTypes, then run bench --site <site> migrate")


if __name__ == "__main__":
	main()
