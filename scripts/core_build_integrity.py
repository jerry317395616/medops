"""Attest pinned Core source and exact generated assets without resetting either."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

CORE_COMMIT = "8c697bafd30aebd418aee18f618b287bbab557f3"
MOBILE = "ione_core/public/mobile/"
ROUTE = "ione_core/www/ione-mobile.html"


class IntegrityError(RuntimeError):
	"""A source or sealed build changed unexpectedly."""


def git(app: Path, *args: str) -> bytes:
	return subprocess.run(["git", "-C", str(app), *args], check=True, capture_output=True).stdout


def generated(path: str) -> bool:
	if path in {MOBILE + "index.html", MOBILE + "manifest.webmanifest", MOBILE + "service-worker.js", ROUTE}:
		return True
	return bool(re.fullmatch(re.escape(MOBILE) + r"assets/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\.(?:js|css)", path))


def checked_file(app: Path, relative: str) -> Path:
	parts = PurePosixPath(relative).parts
	if not parts or PurePosixPath(relative).is_absolute() or any(part in {".", ".."} for part in parts):
		raise IntegrityError("Unsafe repository path")
	path = app
	for part in parts:
		path = path / part
		if path.is_symlink():
			raise IntegrityError("Symlinks are not permitted in the Core inventory")
	if path.exists() and not path.is_file():
		raise IntegrityError("Non-file in the Core inventory")
	return path


def paths(app: Path, *args: str) -> list[str]:
	return [item.decode("utf-8") for item in git(app, *args).split(b"\0") if item]


def validate_source(app: Path, *, expected_head: str = CORE_COMMIT, clean: bool = False) -> None:
	if git(app, "rev-parse", "HEAD").decode().strip() != expected_head:
		raise IntegrityError("Core is not at the pinned commit")
	# The build never stages files. Even generated index changes must fail.
	if git(app, "diff", "--cached", "--name-only", "-z"):
		raise IntegrityError("Core Git index changed")
	changed = paths(app, "diff", "HEAD", "--name-only", "--no-renames", "-z")
	changed += paths(app, "ls-files", "--others", "--exclude-standard", "-z")
	if any(clean or not generated(path) for path in changed):
		raise IntegrityError("Core source changed outside the explicit generated outputs")
	for path in changed:
		checked_file(app, path)


def inventory(app: Path) -> dict[str, dict[str, str | bool]]:
	result: dict[str, dict[str, str | bool]] = {}
	relatives = set(paths(app, "ls-files", "--cached", "--others", "--exclude-standard", "-z"))
	# Inventory every generated file, including files matched by Git ignore rules.
	for path in (app / MOBILE).rglob("*"):
		relative = path.relative_to(app).as_posix()
		if path.is_symlink() or not path.is_dir():
			if not generated(relative):
				raise IntegrityError("Unexpected file in the generated Core output")
			relatives.add(relative)
	for relative in sorted(relatives):
		path = checked_file(app, relative)
		if path.exists():
			result[relative] = {
				"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
				"executable": bool(path.stat().st_mode & stat.S_IXUSR),
			}
	return result


def proof_path(app: Path, proof: Path) -> None:
	if proof.resolve().is_relative_to(app.resolve()):
		raise IntegrityError("Build proof must be outside the Core checkout")
	if proof.is_symlink():
		raise IntegrityError("Build proof cannot be a symlink")


def seal(app: Path, proof: Path, *, expected_head: str = CORE_COMMIT) -> str:
	proof_path(app, proof)
	if proof.exists():
		raise IntegrityError("Refusing to overwrite an existing build proof")
	validate_source(app, expected_head=expected_head)
	entry = checked_file(app, MOBILE + "index.html").read_bytes()
	if not entry or entry != checked_file(app, ROUTE).read_bytes():
		raise IntegrityError("Generated Core route does not match the build entry")
	for name in ("manifest.webmanifest", "service-worker.js"):
		if (
			checked_file(app, MOBILE + name).read_bytes()
			!= checked_file(app, "frontend/public/" + name).read_bytes()
		):
			raise IntegrityError("Generated Core public file differs from its pinned source")
	files = inventory(app)
	if not any(path.startswith(MOBILE + "assets/") and path.endswith(".js") for path in files):
		raise IntegrityError("Core build is missing JavaScript assets")
	data = json.dumps({"schema_version": 1, "commit": expected_head, "files": files}, sort_keys=True).encode()
	with proof.open("xb") as stream:
		stream.write(data)
	return hashlib.sha256(data).hexdigest()


def verify(app: Path, proof: Path, digest: str, *, expected_head: str = CORE_COMMIT) -> None:
	proof_path(app, proof)
	data = proof.read_bytes()
	if not re.fullmatch(r"[a-f0-9]{64}", digest) or hashlib.sha256(data).hexdigest() != digest:
		raise IntegrityError("Core build proof digest changed")
	payload = json.loads(data)
	if payload.get("schema_version") != 1 or payload.get("commit") != expected_head:
		raise IntegrityError("Core build proof does not match the pinned revision")
	validate_source(app, expected_head=expected_head)
	if payload.get("files") != inventory(app):
		raise IntegrityError("Core source or generated files changed after the build was sealed")


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("phase", choices=("prepare", "seal", "verify"))
	parser.add_argument("--app", type=Path, required=True)
	parser.add_argument("--proof", type=Path)
	parser.add_argument("--digest")
	args = parser.parse_args()
	if args.phase == "prepare":
		validate_source(args.app, clean=True)
	elif args.proof is None:
		parser.error("seal/verify requires --proof")
	elif args.phase == "seal":
		print(seal(args.app, args.proof))
	elif args.digest is None:
		parser.error("verify requires the pre-test --digest")
	else:
		verify(args.app, args.proof, args.digest)
		print("Pinned Core source and sealed build inventory verified")


if __name__ == "__main__":
	main()
