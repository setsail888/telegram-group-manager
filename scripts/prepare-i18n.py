"""One-time lossless UI marking and a reproducible translation-source inventory.

Use --apply only when migrating new source files. --check checks catalog coverage.
"""
import ast
from collections import Counter
import json
from pathlib import Path
from string import Formatter
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = ("admin_ui", "commands", "economy_commands", "flood_ui", "member_ui", "points", "service",
         "word_ui", "economy", "store", "raffle", "scheduler", "reply_rules", "variants", "word_library",
         "flood_penalty", "moderation", "language_ui", "command_menu", "cross_flood", "feature_help")


def chinese(value):
    return any("\u3400" <= c <= "\u9fff" for c in value)


class Mark(ast.NodeTransformer):
    def visit_JoinedStr(self, node):
        fields, parts = [], []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(part.value.replace("{", "{{").replace("}", "}}"))
            else:
                name = "p" + str(len(fields))
                fields.append(ast.keyword(arg=name, value=self.visit(part.value)))
                suffix = "!"+chr(part.conversion) if part.conversion != -1 else ""
                if part.format_spec:
                    if not all(isinstance(x, ast.Constant) for x in part.format_spec.values):
                        raise ValueError("Dynamic f-string format spec needs a manual translation marker")
                    suffix += ":"+"".join(x.value for x in part.format_spec.values)
                parts.append("{"+name+suffix+"}")
        return ast.Call(func=ast.Name(id="_ui", ctx=ast.Load()), args=[ast.Constant(value="".join(parts))], keywords=fields)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and chinese(node.value):
            return ast.Call(func=ast.Name(id="_ui", ctx=ast.Load()), args=[node], keywords=[])
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in ("_ui", "join_text"):
            return node
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join" and len(node.args) == 1:
            return ast.Call(func=ast.Name(id="join_text", ctx=ast.Load()), args=[node.func.value, *node.args], keywords=[])
        return node


def mark_source(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Keep formatting and comments: replace only outermost affected expressions.
    lines = source.splitlines(keepends=True)
    offsets, total = [], 0
    for line in lines:
        offsets.append(total)
        total += len(line.encode("utf-8"))
    replacements = []
    def walk(node):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # docstrings are documentation, not UI
        eligible = (isinstance(node, ast.JoinedStr) or isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and chinese(node.value)
                    or isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_ui", "join_text"):
            return
        if eligible:
            replacement = ast.unparse(Mark().visit(node))
            replacements.append((offsets[node.lineno-1]+node.col_offset, offsets[node.end_lineno-1]+node.end_col_offset, replacement))
        else:
            for child in ast.iter_child_nodes(node):
                walk(child)
    walk(tree)
    raw = source.encode("utf-8")
    for start, end, value in sorted(replacements, reverse=True):
        raw = raw[:start]+value.encode("utf-8")+raw[end:]
    source = raw.decode("utf-8")
    if "from .i18n import" not in source:
        pos = source.find("\n", source.find('"""', 3)+3)+1 if source.startswith('"""') else 0
        source = source[:pos]+"from .i18n import msg as _ui, join_text\n"+source[pos:]
    ast.parse(source)
    path.write_text(source, encoding="utf-8", newline="\n")


def inventory():
    result = {}
    for name in FILES:
        path = ROOT / "app" / (name+".py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_ui"
                    and node.args and isinstance(node.args[0], ast.Constant) and chinese(node.args[0].value)):
                result.setdefault(node.args[0].value, []).append(f"app/{name}.py:{node.lineno}")
    # Only trusted, shipped pack descriptions are translatable. Keywords are data.
    for filename in ("curated.json", "short-words.json"):
        path = ROOT / "app" / "wordlists" / filename
        packs = json.loads(path.read_text(encoding="utf-8"))
        for pack in packs if isinstance(packs, list) else [packs]:
            for field in ("name", "description"):
                result.setdefault(pack[field], []).append(f"app/wordlists/{filename}:{pack['id']}:{field}")
    return result


def placeholders(template):
    return Counter((field, spec, conversion) for _, field, spec, conversion in Formatter().parse(template)
                   if field is not None)


if __name__ == "__main__":
    if "--apply" in sys.argv:
        for file in FILES:
            mark_source(ROOT / "app" / (file+".py"))
    sources = inventory()
    target = ROOT / "app" / "locales"
    target.mkdir(exist_ok=True)
    (target / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
    print(f"{len(sources)} translation templates")
    if "--check" in sys.argv:
        sys.path.insert(0, str(ROOT))
        from app.i18n import LOCALES
        for locale in LOCALES:
            if locale == "zh-CN":
                continue
            path = target / f"{locale}.json"
            if not path.is_file():
                raise SystemExit(f"Missing locale catalog: {locale}")
            translated = json.loads(path.read_text(encoding="utf-8"))
            missing = set(sources)-set(translated)
            if missing:
                print(locale, "Missing translations:", len(missing))
                for source in sorted(missing):
                    print(repr(source))
                raise SystemExit(1)
            invalid = [source for source in sources if not isinstance(translated[source], str)
                       or not translated[source].strip()
                       or placeholders(source) != placeholders(translated[source])]
            if invalid:
                for source in invalid:
                    print(locale, "Empty translation or placeholder mismatch:", repr(source), repr(translated[source]))
                raise SystemExit(1)
            print(locale, "catalog coverage and placeholders OK")
