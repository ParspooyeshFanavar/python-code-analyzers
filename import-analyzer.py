#!/usr/bin/env python3

import argparse
import ast
import json
import os
import re
import sys
import warnings
from functools import lru_cache
from os.path import isdir, isfile, join, realpath, split

import tomllib

warnings.simplefilter("ignore")

parser = argparse.ArgumentParser(
	prog=sys.argv[0],
	add_help=False,
	# allow_abbrev=False,
)
parser.add_argument(
	"-o",
	"--out-dir",
	dest="out_dir",
	default=".",
)
parser.add_argument(
	"scan_dir",
	action="store",
	default=".",
	nargs="?",
)
args = parser.parse_args()

scanDir = realpath(args.scan_dir)

rootDir = scanDir
scanDirParts = scanDir.split("/")
for count in range(len(scanDirParts), 1, -1):
	_testDir = join("/", *scanDirParts[:count])
	if isfile(join(_testDir, "pyproject.toml")):
		rootDir = _testDir
if not rootDir.endswith("/"):
	rootDir += "/"
print(f"Root Dir: {rootDir}")

with open(join(rootDir, "pyproject.toml"), "rb") as _file:
	full_config = tomllib.load(_file)
tool_config = full_config.get("tool") or {}
config = tool_config.get("import-analyzer") or {}

re_exclude_list = [re.compile("^" + pat) for pat in config.get("exclude", [])]
exclude_toplevel_module = set(config.get("exclude_toplevel_module", []))

full_data = {}

imported_set = set()
imported_from_by_module_and_path = {}

all_module_attr_access = set()


@lru_cache(maxsize=None, typed=False)
def is_excluded(fpath: str) -> bool:
	for pat in re_exclude_list:
		if pat.match(fpath):
			return True
	return False

def formatList(lst):
	return json.dumps(lst)

@lru_cache(maxsize=None, typed=False)
def moduleFilePath(
	module,
	dirPathRel,
	subDirs,
	files,
	silent=False,
):
	if not module:
		return None
	parts = module.split(".")
	if not parts:
		return None
	main = parts[0]
	if main in sys.stdlib_module_names:
		return None
	if main in exclude_toplevel_module:
		return None
	if main in files or main + ".py" in files or main in subDirs:
		parts = list(split(dirPathRel)) + parts
	else:
		try:
			mod = __import__(main)
		except ModuleNotFoundError:
			pass
		except Exception as e:
			print(f"error importing {main}: {e}", file=sys.stderr)
		else:
			if "/site-packages/" in mod.__file__:
				return None

	pathRel = join(*parts)
	dpath = join(rootDir, pathRel)
	if isdir(dpath):
		return None
	if isfile(dpath + ".py"):
		return pathRel + ".py"
	if not silent:
		print(f"{module=}, {pathRel=}, {dirPathRel=}", file=sys.stderr)
	return None


def find__all__(code):
	for stm in code.body:
		if not isinstance(stm, ast.Assign):
			continue
		target = stm.targets[0]
		if not isinstance(target, ast.Name):
			continue
		# print(target)
		if target.id != "__all__":
			continue
		assert isinstance(stm.value, ast.List)
		assert len(stm.targets) == 1
		# stm.value.elts[i]: ast.Constant
		return stm, [elem.value for elem in stm.value.elts]
	return None, []


for dirPath, subDirs, files in os.walk(scanDir):
	dirPathRel = dirPath[len(rootDir) :]

	for fname in files:
		if not fname.endswith(".py"):
			continue
		fpath = join(dirPath, fname)
		if is_excluded(fpath):
			continue
		# print(fpath)
		# strip rootDir prefix
		fpathRel = fpath[len(rootDir) :]

		if is_excluded(fpathRel):
			continue
		# print(f"{fpathRel = }")

		imports = []
		imports_by_name = {}
		import_froms = []
		attr_access = set()

		def handleImport(stm):
			for name in stm.names:
				module_fpath = moduleFilePath(
					name.name,
					dirPathRel,
					tuple(subDirs),
					tuple(files),
				)
				if name.asname:
					imports.append(f"{name.name} as {name.asname}")
					imports_by_name[name.asname] = (name.name, module_fpath)
				else:
					imports.append(name.name)
					imports_by_name[name.name] = (name.name, module_fpath)
				imported_set.add(name.name)

		def handleImportFrom(stm):
			module = stm.module
			if module is None:
				# print(f"{module = }, {stm!r}", file=sys.stderr)
				return
			jsonNames = []
			module_fpath = moduleFilePath(
				module,
				dirPathRel,
				tuple(subDirs),
				tuple(files),
			)
			try:
				import_froms_set = imported_from_by_module_and_path[
					(module, module_fpath)
				]
			except KeyError:
				import_froms_set = imported_from_by_module_and_path[
					(module, module_fpath)
				] = set()
			for name in stm.names:
				if not name.name:
					# print(f"{name = }", file=sys.stderr)
					continue
				full_name = module + "." + name.name
				module_fpath = moduleFilePath(
					full_name,
					dirPathRel,
					tuple(subDirs),
					tuple(files),
					silent=True,
				)
				if name.asname:
					jsonNames.append(f"{name.name} as {name.asname}")
					imports_by_name[name.asname] = (full_name, module_fpath)
				else:
					jsonNames.append(name.name)
					imports_by_name[name.name] = (full_name, module_fpath)
				import_froms.append((module, jsonNames))
				import_froms_set.add(name.name)

		def handleAttribute(stm):
			if isinstance(stm.value, ast.Name):
				attr_access.add((stm.value.id, stm.attr))
				return None
			return handleStatement(stm.value)

		def handleStatementList(statements):
			for stm in statements:
				handleStatement(stm)

		def handleStatements(*statements):
			for stm in statements:
				handleStatement(stm)

		def handleStatement(stm):
			if stm is None:
				return None
			if isinstance(stm, ast.Import):
				handleImport(stm)
			elif isinstance(stm, ast.ImportFrom):
				handleImportFrom(stm)
			elif isinstance(stm, ast.Name):
				# print(f"name: id={stm.id}")
				pass
			elif isinstance(
				stm,
				ast.Pass
				| ast.Break
				| ast.Continue
				| ast.Delete
				| ast.Constant
				| ast.JoinedStr
				| ast.Slice
				| ast.Global,
			):
				pass
			elif isinstance(stm, ast.Assign):
				handleStatement(stm.value)
			elif isinstance(stm, ast.AugAssign):
				handleStatements(stm.target, stm.value)
			elif isinstance(stm, ast.Expr):
				handleStatement(stm.value)
			elif isinstance(stm, ast.Return):
				handleStatement(stm.value)
			elif isinstance(stm, ast.Yield):
				handleStatement(stm.value)
			elif isinstance(stm, ast.YieldFrom):
				handleStatement(stm.value)
			elif isinstance(stm, ast.Assert):
				handleStatements(stm.test, stm.msg)
			elif isinstance(stm, ast.IfExp):
				handleStatements(stm.test, stm.body, stm.orelse)
			elif isinstance(stm, ast.FunctionDef):
				handleStatementList(stm.args.defaults)
				handleStatementList(stm.body)
				handleStatementList(stm.decorator_list)
			elif isinstance(stm, ast.ClassDef):
				handleStatementList(stm.body)
			elif isinstance(stm, ast.BoolOp):
				handleStatementList(stm.values)
			elif isinstance(stm, ast.Subscript):
				handleStatements(stm.value, stm.slice)
			elif isinstance(stm, ast.With):
				handleStatementList(stm.items + stm.body)
			elif isinstance(stm, ast.List | ast.Tuple | ast.Set):
				handleStatementList(stm.elts)
			elif isinstance(stm, ast.Lambda):
				handleStatement(stm.body)
			elif isinstance(stm, ast.For):
				handleStatementList([stm.target, stm.iter] + stm.body + stm.orelse)
			elif isinstance(stm, ast.While):
				handleStatementList([stm.test] + stm.body + stm.orelse)
			elif isinstance(stm, ast.BinOp):
				handleStatements(stm.left, stm.right)
			elif isinstance(stm, ast.UnaryOp):
				handleStatement(stm.operand)
			elif isinstance(stm, ast.Try):
				handleStatementList(
					stm.body + stm.handlers + stm.orelse + stm.finalbody,
				)
			elif isinstance(stm, ast.ExceptHandler):
				handleStatementList([stm.type] + stm.body)
			elif isinstance(stm, ast.Call):
				handleStatement(stm.func)
			elif isinstance(stm, ast.If):
				handleStatementList([stm.test] + stm.body)
			elif isinstance(stm, ast.Compare):
				handleStatementList([stm.left] + stm.comparators)
			elif isinstance(stm, ast.withitem):
				handleStatements(stm.context_expr, stm.optional_vars)
			elif isinstance(stm, ast.Raise):
				handleStatement(stm.exc)
			elif isinstance(stm, ast.Return):
				handleStatement(stm.value)
			elif isinstance(stm, ast.Dict):
				# TODO
				pass
			elif isinstance(
				stm,
				ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp,
			):
				handleStatementList(stm.generators)
			elif isinstance(stm, ast.comprehension):
				handleStatementList([stm.target, stm.iter] + stm.ifs)
			elif isinstance(stm, ast.Attribute):
				return handleAttribute(stm)
			elif isinstance(stm, ast.AnnAssign):
				handleStatements(stm.target, stm.annotation, stm.value)
			elif isinstance(stm, ast.NamedExpr):
				handleStatements(stm.target, stm.value)
			elif isinstance(stm, ast.AsyncFunctionDef):
				handleStatementList(stm.args.defaults)
				handleStatementList(stm.body)
				handleStatementList(stm.decorator_list)
			elif isinstance(stm, ast.Starred):
				handleStatement(stm.value)
			elif isinstance(stm, ast.Nonlocal):
				# stm.names is list[str]
				pass
			else:
				print(f"Unknown statemnent type: {stm} with type {type(stm)}")

		with open(fpath) as _file:
			text = _file.read()
		try:
			code = ast.parse(text)
		except Exception as e:
			print(f"failed to parse {fpath}: {e}", file=sys.stderr)
			continue
		for stm in code.body:
			if isinstance(stm, ast.Import):
				handleImport(stm)
				continue

			if isinstance(stm, ast.ImportFrom):
				handleImportFrom(stm)
				continue

			handleStatement(stm)

		module_attr_access = set()
		for _id, attr in attr_access:
			if _id in ("self", "msg"):
				continue
			if _id not in imports_by_name:
				# print(f"{fpathRel}: {_id}.{attr}  (Unknown)")
				continue
			module, module_fpath = imports_by_name[_id]
			# print(f"{fpathRel}: {module}.{attr} from file ({module_fpath})")
			module_attr_access.add((module, attr, module_fpath))
			all_module_attr_access.add((module, attr, module_fpath))

		# print(json.dumps(list(attr_access)))

		full_data[fpathRel] = {
			"imports": imports,
			"import_froms": import_froms,
			"module_attr_access": list(module_attr_access),
		}

to_check_imported_modules = set()
for (module, module_fpath), _used_names in imported_from_by_module_and_path.items():
	if module_fpath is None:
		continue
	to_check_imported_modules.add((module, module_fpath))


for module, _attr, module_fpath in all_module_attr_access:
	if module_fpath is None:
		continue
	to_check_imported_modules.add((module, module_fpath))


module_attr_access_by_fpath = {}
for _module, attr, module_fpath in all_module_attr_access:
	if module_fpath is None:
		continue
	try:
		attrs = module_attr_access_by_fpath[module_fpath]
	except KeyError:
		attrs = module_attr_access_by_fpath[module_fpath] = set()
	attrs.add(attr)


for module, module_fpath in sorted(to_check_imported_modules):
	# print(module, module_fpath)
	full_path = join(rootDir, module_fpath)
	with open(full_path) as _file:
		text = _file.read()
	if is_excluded(module_fpath):
		continue
	module_top = module_fpath.split("/")[0]
	if module_top in exclude_toplevel_module:
		continue
	try:
		code = ast.parse(text)
	except Exception as e:
		print(f"failed to parse {module_fpath=} {fpath=}: {e}", file=sys.stderr)
		continue
	_all_stm, _all = find__all__(code)
	_all_set = set(_all)
	_all_set_current = _all_set.copy()
	names1 = imported_from_by_module_and_path.get((module, module_fpath))
	if names1:
		_all_set.update(names1)
	names2 = module_attr_access_by_fpath.get(module_fpath, None)
	if names2:
		_all_set.update(names2)
	if "*" in _all_set:
		_all_set.remove("*")
	if len(_all_set) == len(_all_set_current):
		continue
	add_list = list(_all_set.difference(_all_set_current))

	if _all_set_current:
		print(module_fpath)
		print("ADD to __all__:", formatList(add_list))
		print()
	else:
		print(module_fpath)
		print("__all__ =", formatList(add_list))
		print()

	# if _all_stm is not None:
	# 	_all_stm.value.elts = [
	# 		ast.Constant(value=value)
	# 		for value in _all
	# 	]
	# else:
	# 	for index, stm in enumerate(code.body):
	# 		if isinstance(stm, (
	# 			ast.Assign,
	# 			ast.FunctionDef,
	# 			ast.ClassDef,
	# 		)):
	# 			break
	# 	code.body.insert(index, ast.Assign(
	# 		targets=[ast.Name("__all__", None)],
	# 		value=ast.List(elts=[
	# 			ast.Constant(value=value)
	# 			for value in _all
	# 		]),
	# 	))

	# lines = text.split("\n")
	# for line in lines:
	#
	# code_formatted = ast.unparse(code)
	# with open(full_path, mode="w") as _file:
	# 	_file.write(code_formatted)
	# print("Updated", full_path)


with open(f"{args.out_dir}/module-attrs.json", "w") as _file:
	json.dump(
		{
			module: sorted(attrs)
			for module, attrs in module_attr_access_by_fpath.items()
		},
		_file,
		indent="\t",
		sort_keys=True,
	)


with open(f"{args.out_dir}/imports_set.json", "w") as _file:
	json.dump(sorted(imported_set), _file, indent="\t")

with open(f"{args.out_dir}/imports_from_set.json", "w") as _file:
	json.dump(
		{
			module: sorted(value)
			for (
				module,
				module_fpath,
			), value in imported_from_by_module_and_path.items()
			if module_fpath
		},
		_file,
		indent="\t",
		sort_keys=True,
	)
