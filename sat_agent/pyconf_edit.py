"""Surgical, formatting-preserving edits of SAT .pyconf files.

pyconf is sat's own DSL (sat/src/pyconf.py): bare or quoted keys, `$refs`,
`+` expressions, `#` comments, mappings `{}` and sequences `[]`. sat can
parse it but cannot write it back without losing comments and layout, so
every edit here is a text splice located with a bracket/string/comment-aware
scanner. All the edits sat-agent makes are *platform-scoped* (see
guard.py), following the sat_salome conventions:

* applications/<APP>.pyconf
    - `platform : [...]` gains the new dist code;
    - `__overwrite__ : [ {__condition__ : "VARS.dist in ['<DIST>']" ...} ]`
      holds every per-platform product choice (version, section, rm_products);
* products/<product>.pyconf
    - `system_info : { "<DIST>" : { apt/apt_dev/rpm/rpm_dev : [...] } }`;
    - extra sections `<section>_<DIST_SUFFIX>` (e.g. default_UB26_04,
      version_v5_6_1_UB26_04) carrying platform patches / options, selected
      from the application's __overwrite__ block.
"""

import re
from dataclasses import dataclass
from pathlib import Path

_KEY_RE = re.compile(r"""(?:"([^"\n]+)"|'([^'\n]+)'|([A-Za-z_][\w.\-]*))\s*:""")
_STR_RE = re.compile(r""""((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'""")


class PyconfEditError(ValueError):
    pass


@dataclass
class Span:
    start: int   # index of the opening bracket
    end: int     # index of the matching closing bracket

    @property
    def inner(self) -> tuple[int, int]:
        return self.start + 1, self.end


class PyconfText:
    def __init__(self, text: str):
        self.text = text
        self._build_mask()

    # ------------------------------------------------------------------ scanning
    def _build_mask(self) -> None:
        """self.code: same length as text, comments blanked, string contents
        replaced by '_' (quotes kept) -> brackets/colons in it are structural."""
        t = self.text
        out = list(t)
        i, n = 0, len(t)
        while i < n:
            c = t[i]
            if c == "#":
                j = t.find("\n", i)
                j = n if j == -1 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
            elif c in "\"'":
                triple = t[i:i + 3] == c * 3
                q = c * 3 if triple else c
                j = i + len(q)
                while j < n:
                    if t[j] == "\\":
                        j += 2
                        continue
                    if t.startswith(q, j):
                        break
                    if not triple and t[j] == "\n":
                        break
                    j += 1
                for k in range(i + len(q), min(j, n)):
                    if out[k] != "\n":
                        out[k] = "_"
                i = j + len(q)
            else:
                i += 1
        self.code = "".join(out)

    def match_close(self, open_idx: int) -> int:
        pairs = {"{": "}", "[": "]", "(": ")"}
        stack = []
        for i in range(open_idx, len(self.code)):
            c = self.code[i]
            if c in pairs:
                stack.append(pairs[c])
            elif c in pairs.values():
                if not stack or stack.pop() != c:
                    raise PyconfEditError(f"unbalanced '{c}' at offset {i}")
                if not stack:
                    return i
        raise PyconfEditError(f"no closing bracket for offset {open_idx}")

    def _depth0_positions(self, start: int, end: int):
        """Yields indexes in [start, end) at bracket depth 0 relative to start."""
        depth = 0
        for i in range(start, end):
            c = self.code[i]
            if c in "{[(":
                if depth == 0:
                    yield i
                depth += 1
            elif c in "}])":
                depth -= 1
            elif depth == 0:
                yield i

    def keys_in(self, start: int, end: int) -> list[tuple[str, int, int]]:
        """(key, key_start, after_colon) for every key at depth 0 of [start,end)."""
        res = []
        depth0 = set(self._depth0_positions(start, end))
        i = start
        while i < end:
            m = _KEY_RE.match(self.text, i)
            if m and i in depth0 and m.end() <= end and self.code[m.end() - 1] == ":" and \
                    (i == start or not (self.code[i - 1].isalnum() or self.code[i - 1] in "_.$")):
                key = m.group(1) or m.group(2) or m.group(3)
                # quoted keys must be real strings, not masked contents
                if (m.group(1) or m.group(2)) and self.code[i] not in "\"'":
                    i += 1
                    continue
                res.append((key, i, m.end()))
                i = self.value_span(m.end(), end)[1]
                continue
            i += 1
        return res

    def value_span(self, after_colon: int, end: int) -> tuple[int, int]:
        """[start, stop) of the value following a key's colon."""
        i = after_colon
        while i < end and self.text[i] in " \t\r\n":
            i += 1
        if i < end and self.code[i] in "{[":
            return i, self.match_close(i) + 1
        j = i
        depth = 0
        while j < end:
            c = self.code[j]
            if c in "{[(":
                depth += 1
            elif c in "}])":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and c in "\n,":
                # a `+` continuation on the next line keeps the expression going
                nxt = self.code[j + 1:end].lstrip(" \t\r\n")
                if c == "\n" and nxt.startswith("+"):
                    j += 1
                    continue
                break
            j += 1
        while j > i and self.code[j - 1] in " \t\r":
            j -= 1
        return i, j

    def find_key(self, key: str, start: int, end: int) -> tuple[int, int, int, int] | None:
        """(key_start, after_colon, value_start, value_end) or None."""
        for k, ks, ac in self.keys_in(start, end):
            if k == key:
                vs, ve = self.value_span(ac, end)
                return ks, ac, vs, ve
        return None

    def block(self, path: list[str]) -> Span:
        start, end = 0, len(self.text)
        span = None
        for key in path:
            found = self.find_key(key, start, end)
            if not found:
                raise PyconfEditError(f"key path {'.'.join(path)!r} not found (missing {key!r})")
            vs, ve = found[2], found[3]
            if self.code[vs] not in "{[":
                raise PyconfEditError(f"{key!r} is not a mapping/sequence")
            span = Span(vs, ve - 1)
            start, end = span.inner
        if span is None:
            raise PyconfEditError("empty key path")
        return span

    def has(self, path: list[str]) -> bool:
        try:
            self.block(path[:-1]) if len(path) > 1 else None
            start, end = (self.block(path[:-1]).inner if len(path) > 1 else (0, len(self.text)))
            return self.find_key(path[-1], start, end) is not None
        except PyconfEditError:
            return False

    def seq_items(self, span: Span) -> list[Span]:
        """Mapping items `{...}` directly inside a sequence span."""
        items = []
        s, e = span.inner
        i = s
        while i < e:
            if self.code[i] == "{":
                j = self.match_close(i)
                items.append(Span(i, j))
                i = j + 1
            elif self.code[i] == "[":
                i = self.match_close(i) + 1
            else:
                i += 1
        return items

    def strings_in(self, start: int, end: int) -> list[str]:
        """Every string literal in [start,end), comments excluded."""
        res = []
        for m in _STR_RE.finditer(self.text, start, end):
            if self.code[m.start()] in "\"'":
                res.append(m.group(1) if m.group(1) is not None else m.group(2))
        return res

    # ------------------------------------------------------------------ helpers
    def line_indent(self, idx: int) -> str:
        ls = self.text.rfind("\n", 0, idx) + 1
        m = re.match(r"[ \t]*", self.text[ls:])
        return m.group(0)

    def child_indent(self, span: Span) -> str:
        s, e = span.inner
        keys = self.keys_in(s, e)
        if keys:
            return self.line_indent(keys[0][1])
        return self.line_indent(span.start) + "    "


# ---------------------------------------------------------------------- rendering

def render_str_list(items: list[str], indent: str = "", width: int = 110) -> str:
    if not items:
        return "[]"
    parts = [f'"{x}"' for x in items]
    lines, cur = [], "["
    for p in parts:
        piece = p if cur == "[" else ", " + p
        if len(indent) + len(cur) + len(piece) > width and cur != "[":
            lines.append(cur + ",")
            cur = " " + p
        else:
            cur += piece
    lines.append(cur + "]")
    return ("\n" + indent).join(lines)


def _replace(text: str, start: int, end: int, new: str) -> str:
    return text[:start] + new + text[end:]


def _insert_before_close(pt: PyconfText, span: Span, block_text: str) -> str:
    """Inserts `block_text` (already indented, no trailing newline) as the
    last entry of mapping `span`."""
    close = span.end
    ls = pt.text.rfind("\n", 0, close) + 1
    if pt.text[ls:close].strip() == "":
        return pt.text[:ls] + block_text + "\n" + pt.text[ls:]
    return pt.text[:close] + "\n" + block_text + "\n" + pt.line_indent(span.start) + pt.text[close:]


def set_key(text: str, path: list[str], key: str, value: str, quote_key: bool = False) -> str:
    """Sets `key : value` in mapping `path` (value is raw pyconf text)."""
    pt = PyconfText(text)
    span = pt.block(path) if path else Span(-1, len(text))
    s, e = span.inner if path else (0, len(text))
    found = pt.find_key(key, s, e)
    if found:
        return _replace(text, found[2], found[3], value)
    if not path:
        return text.rstrip("\n") + f"\n\n{key} :\n{value}\n"
    indent = pt.child_indent(span)
    k = f'"{key}"' if quote_key else key
    return _insert_before_close(pt, span, f"{indent}{k} : {value}")


def get_value_text(text: str, path: list[str], key: str) -> str | None:
    pt = PyconfText(text)
    try:
        s, e = pt.block(path).inner if path else (0, len(text))
    except PyconfEditError:
        return None
    found = pt.find_key(key, s, e)
    return text[found[2]:found[3]] if found else None


def get_str_list(text: str, path: list[str], key: str) -> list[str] | None:
    pt = PyconfText(text)
    try:
        s, e = pt.block(path).inner if path else (0, len(text))
    except PyconfEditError:
        return None
    found = pt.find_key(key, s, e)
    if not found:
        return None
    return pt.strings_in(found[2], found[3])


# ---------------------------------------------------------------------- products

def product_sections(text: str) -> list[str]:
    pt = PyconfText(text)
    return [k for k, _, _ in pt.keys_in(0, len(text))]


def section_text(text: str, section: str) -> str:
    pt = PyconfText(text)
    found = pt.find_key(section, 0, len(text))
    if not found:
        raise PyconfEditError(f"section {section!r} not found")
    return text[found[2]:found[3]]


def add_section(text: str, section: str, body: str, after: str | None = None, before: str | None = None) -> str:
    """Adds a top-level `section :\\n{ ... }` (body = full `{...}` text),
    right after section `after` / before section `before` when given, else at
    the end of the file."""
    pt = PyconfText(text)
    if pt.find_key(section, 0, len(text)):
        raise PyconfEditError(f"section {section!r} already exists")
    block = f"{section} :\n{body}\n"
    if before:
        found = pt.find_key(before, 0, len(text))
        if found:
            ls = text.rfind("\n", 0, found[0]) + 1
            return text[:ls] + block + "\n" + text[ls:]
    if after:
        found = pt.find_key(after, 0, len(text))
        if found:
            pos = found[3]
            return text[:pos] + "\n\n" + block.rstrip("\n") + text[pos:]
    return text.rstrip("\n") + "\n\n" + block


def clone_section(text: str, src: str, dst: str) -> str:
    return add_section(text, dst, section_text(text, src), after=src)


_PLATFORM_SUFFIX_RE = re.compile(r"_[A-Z]{2,}\d+(?:_\d+)?$")


def platform_section_name(section: str, suffix: str) -> str:
    """Name of the new platform's variant of a section: default -> default_FD45,
    version_4_2_0_FD44 -> version_4_2_0_FD45 (the reference's suffix is replaced)."""
    if section.endswith("_" + suffix):
        return section
    return _PLATFORM_SUFFIX_RE.sub("", section) + "_" + suffix


def is_incremental(text: str) -> bool:
    v = get_value_text(text, ["default", "properties"], "incremental")
    return bool(v) and v.strip().strip("'\"") == "yes"


def section_system_info(text: str, section: str, keys: tuple[str, ...]) -> dict[str, list[str]]:
    """The generic system_info lists a product gets with `section` (an
    incremental section's system_info replaces the default one as a whole)."""
    src = section if get_value_text(text, [section], "system_info") is not None else "default"
    return {k: get_str_list(text, [src, "system_info"], k) or [] for k in keys}


def set_section_system_info(text: str, section: str, lists: dict[str, list[str]]) -> str:
    """Sets the whole `system_info` mapping of a section."""
    pt = PyconfText(text)
    ind = pt.child_indent(pt.block([section]))
    width = max(len(k) for k in lists)
    body = "".join(f"{ind}    {k.ljust(width)} : {render_str_list(v, ind + ' ' * (width + 7))}\n"
                   for k, v in lists.items())
    return set_key(text, [section], "system_info", f"\n{ind}{{\n{body}{ind}}}")


def add_system_info_section(text: str, section: str, lists: dict[str, list[str]], after: str | None = None,
                            before: str | None = None) -> str:
    """Adds a section holding only a system_info mapping (incremental products), the
    sat_salome form of a platform's package names:  default_FD44 : { system_info : { rpm : [...] } }."""
    width = max(len(k) for k in lists)
    body = "".join(f"        {k.ljust(width)} : {render_str_list(v, ' ' * (width + 11))}\n" for k, v in lists.items())
    return add_section(text, section, "{\n    system_info :\n    {\n" + body + "    }\n}", after=after, before=before)


def section_patches(text: str, section: str) -> list[str] | None:
    return get_str_list(text, [section], "patches")


def set_section_list(text: str, section: str, key: str, items: list[str]) -> str:
    pt = PyconfText(text)
    span = pt.block([section])
    indent = pt.child_indent(span)
    return set_key(text, [section], key, render_str_list(items, indent + " " * (len(key) + 3)))


def system_info_dists(text: str) -> list[str]:
    pt = PyconfText(text)
    try:
        s, e = pt.block(["default", "system_info"]).inner
    except PyconfEditError:
        return []
    # platform blocks are named by sat codes (uppercase: "UB24.04", "FD44", "AR"); package-name
    # keys are lowercase (rpm, apt_dev, pacman, apk..., <key>_exclude)
    return [k for k, _, _ in pt.keys_in(s, e) if k[:1].isupper()]


def clone_system_info_block(text: str, ref: str, dist: str) -> str:
    """Copies system_info."<ref>" as system_info."<dist>" (right after it)."""
    pt = PyconfText(text)
    si = pt.block(["default", "system_info"])
    s, e = si.inner
    if pt.find_key(dist, s, e):
        return text
    found = pt.find_key(ref, s, e)
    if not found:
        raise PyconfEditError(f"system_info has no {ref!r} block")
    ks, _, vs, ve = found
    indent = pt.line_indent(ks)
    body = text[vs:ve]
    return text[:ve] + f'\n{indent}"{dist}" :\n{indent}' + body + text[ve:]


def set_system_info_list(text: str, dist: str, key: str, pkgs: list[str]) -> str:
    """Sets system_info."<dist>".<key> = pkgs, creating the dist block if needed."""
    pt = PyconfText(text)
    si = pt.block(["default", "system_info"])
    s, e = si.inner
    if not pt.find_key(dist, s, e):
        indent = pt.child_indent(si)
        block = f'{indent}"{dist}" :\n{indent}{{\n{indent}}}'
        text = _insert_before_close(pt, si, block)
        pt = PyconfText(text)
    blk = pt.block(["default", "system_info", dist])
    indent = pt.child_indent(blk)
    if indent == pt.line_indent(blk.start) + "    " and not pt.keys_in(*blk.inner):
        indent = pt.line_indent(blk.start) + "    "
    return set_key(text, ["default", "system_info", dist], key,
                   render_str_list(pkgs, indent + " " * (len(key) + 3)))


def get_system_info_list(text: str, dist: str | None, key: str) -> list[str]:
    path = ["default", "system_info"] + ([dist] if dist else [])
    return get_str_list(text, path, key) or []


# ---------------------------------------------------------------------- applications

_COND_RE = re.compile(r"""VARS\.dist\s+in\s+\[([^\]]*)\]""")


def app_platforms(text: str) -> list[str]:
    return get_str_list(text, ["APPLICATION"], "platform") or []


def app_add_platform(text: str, dist: str) -> str:
    plats = app_platforms(text)
    if dist in plats:
        return text
    pt = PyconfText(text)
    s, e = pt.block(["APPLICATION"]).inner
    found = pt.find_key("platform", s, e)
    if not found:
        return set_key(text, ["APPLICATION"], "platform", render_str_list([dist]))
    vs, ve = found[2], found[3]
    close = ve - 1
    # append before the closing bracket, keeping the original style
    inner = text[vs + 1:close].rstrip()
    sep = ", " if inner.strip() else ""
    return text[:vs + 1] + inner + f'{sep}"{dist}"' + text[close:]


@dataclass
class OverwriteBlock:
    span: Span
    condition: str | None
    dists: list[str]          # parsed from "VARS.dist in [...]" (empty if other condition)


def app_overwrite_blocks(text: str) -> list[OverwriteBlock]:
    pt = PyconfText(text)
    found = pt.find_key("__overwrite__", 0, len(text))
    if not found:
        return []
    seq = Span(found[2], found[3] - 1)
    res = []
    for item in pt.seq_items(seq):
        s, e = item.inner
        cond = pt.find_key("__condition__", s, e)
        cond_text = None
        dists: list[str] = []
        if cond:
            strs = pt.strings_in(cond[2], cond[3])
            cond_text = strs[0] if strs else text[cond[2]:cond[3]]
            m = _COND_RE.search(cond_text)
            if m and cond_text.strip().startswith("VARS.dist"):
                dists = re.findall(r"""['"]([^'"]+)['"]""", m.group(1))
        res.append(OverwriteBlock(item, cond_text, dists))
    return res


def _dedicated_block(text: str, dist: str) -> OverwriteBlock | None:
    for b in app_overwrite_blocks(text):
        if b.dists == [dist]:
            return b
    return None


def app_inherit_overwrites(text: str, ref: str, dist: str) -> str:
    """Makes the new platform inherit the reference platform's __overwrite__
    choices without touching what any other platform resolves to:
    * a block dedicated to ref is duplicated for dist, right after it;
    * a block shared by several platforms including ref gets dist added to
      its condition list."""
    if _dedicated_block(text, dist):
        return text
    blocks = app_overwrite_blocks(text)
    # walk backwards so earlier offsets stay valid
    for b in reversed(blocks):
        if ref not in b.dists or dist in b.dists:
            continue
        if b.dists == [ref]:
            body = text[b.span.start:b.span.end + 1]
            body = re.sub(r"""(['"])%s\1""" % re.escape(ref), lambda m: f"{m.group(1)}{dist}{m.group(1)}",
                          body, count=1)
            indent = PyconfText(text).line_indent(b.span.start)
            text = text[:b.span.end + 1] + f"\n{indent}#\n{indent}" + body + text[b.span.end + 1:]
        else:
            pt = PyconfText(text)
            s, e = b.span.inner
            cond = pt.find_key("__condition__", s, e)
            seg = text[cond[2]:cond[3]]
            m = _COND_RE.search(seg)
            lst = m.group(1)
            q = "'" if "'" in lst else '"'
            new_lst = lst.rstrip() + f", {q}{dist}{q}"
            seg2 = seg[:m.start(1)] + new_lst + seg[m.end(1):]
            text = text[:cond[2]] + seg2 + text[cond[3]:]
    if not _dedicated_block(text, dist):
        text = app_ensure_dedicated_block(text, dist)
    return text


def app_ensure_dedicated_block(text: str, dist: str) -> str:
    if _dedicated_block(text, dist):
        return text
    pt = PyconfText(text)
    found = pt.find_key("__overwrite__", 0, len(text))
    if not found:
        return text.rstrip("\n") + (f"\n__overwrite__ :\n[\n    {{\n        __condition__ : \"VARS.dist in ['{dist}']\"\n"
                                    f"    }}\n]\n")
    seq = Span(found[2], found[3] - 1)
    items = pt.seq_items(seq)
    indent = pt.line_indent(items[-1].start) if items else "    "
    block = (f"{indent}#\n{indent}{{\n{indent}    __condition__ : \"VARS.dist in ['{dist}']\"\n{indent}}}")
    close = seq.end
    ls = text.rfind("\n", 0, close) + 1
    return text[:ls] + block + "\n" + text[ls:]


def app_overwrite_get(text: str, dist: str, key: str) -> str | None:
    b = _dedicated_block(text, dist)
    if not b:
        return None
    pt = PyconfText(text)
    found = pt.find_key(key, *b.span.inner)
    return text[found[2]:found[3]] if found else None


def app_overwrite_set(text: str, dist: str, key: str, value: str) -> str:
    """Sets 'key' : value (raw pyconf text) in the block dedicated to dist."""
    text = app_ensure_dedicated_block(text, dist)
    b = _dedicated_block(text, dist)
    pt = PyconfText(text)
    s, e = b.span.inner
    found = pt.find_key(key, s, e)
    if found:
        return text[:found[2]] + value + text[found[3]:]
    indent = pt.child_indent(b.span)
    return _insert_before_close(pt, b.span, f"{indent}'{key}' : {value}")


def app_rm_products(text: str, dist: str) -> list[str]:
    v = app_overwrite_get(text, dist, "APPLICATION.rm_products")
    if not v:
        return []
    return re.findall(r"""['"]([^'"]+)['"]""", v)


def app_add_rm_product(text: str, dist: str, product: str) -> str:
    cur = app_rm_products(text, dist)
    if product in cur:
        return text
    items = ", ".join(f"'{p}'" for p in cur + [product])
    return app_overwrite_set(text, dist, "APPLICATION.rm_products", f"[{items}]")


def product_spec_text(tag: str, section: str | None = None, extra: dict | None = None) -> str:
    """pyconf text for an application product entry, e.g.
    {tag: '2.4.1', base: 'no', section: 'version_2_4_1_UB26_04'}."""
    if not section and not extra:
        return f"'{tag}'"
    parts = [f"tag: '{tag}'", "base: 'no'"]
    if section:
        parts.append(f"section: '{section}'")
    for k, v in (extra or {}).items():
        parts.append(f"{k}: '{v}'")
    return "{" + ", ".join(parts) + "}"


def app_select_section(text: str, dist: str, product: str, section: str, default_tag: str | None = None) -> str:
    """Selects `section` for `product` in the platform's __overwrite__ block, keeping
    the version the platform uses today: {tag: '<tag>', base: 'no', section: '<section>'}."""
    key = f"APPLICATION.products.{product}"
    cur = app_overwrite_get(text, dist, key) or get_value_text(text, ["APPLICATION", "products"], product) or ""
    if f"section: '{section}'" in cur.replace('"', "'"):
        return text
    tag = None
    m = re.search(r"tag\s*:\s*['\"]([^'\"]+)['\"]", cur)
    if m:
        tag = m.group(1)
    elif re.fullmatch(r"\s*['\"][^'\"]+['\"]\s*", cur):
        tag = cur.strip().strip("'\"")
    tag = tag or default_tag or (get_value_text(text, ["APPLICATION"], "tag") or "'master'").strip("'\" ")
    extra = {}
    hm = re.search(r"hpc\s*:\s*['\"](\w+)['\"]", cur)
    if hm:
        extra["hpc"] = hm.group(1)
    return app_overwrite_set(text, dist, key, product_spec_text(tag, section, extra))


def edit_file(path: Path, fn, *args, **kwargs) -> bool:
    """Applies fn(text, *args) to a file; returns True if it changed."""
    old = path.read_text(encoding="utf-8")
    new = fn(old, *args, **kwargs)
    if new != old:
        path.write_text(new, encoding="utf-8")
        return True
    return False
