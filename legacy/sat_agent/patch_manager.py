"""PatchManager: Generates unified diff patches for SALOME product source errors and registers them under SAT_SALOME/products/patches/."""

import difflib
from pathlib import Path
import re

from sat_agent.pyconf_manager import PyconfManager

_PATCHES_LINE_RE = re.compile(r"patches\s*:\s*\[([^\]]*)\]")
_DEFAULT_BLOCK_RE = re.compile(r"default\s*:\s*\{")


class PatchManager:
    """Manages patch generation and registration under SAT_SALOME/products/patches/."""

    def __init__(self, sat_root: Path | str):
        self.sat_root = Path(sat_root)
        self.patches_dir = self.sat_root / "products" / "patches"

    def generate_diff(self, relative_file_path: str, original_lines: list[str], fixed_lines: list[str]) -> str:
        """Generates a unified diff string for patch creation."""
        diff = difflib.unified_diff(
            original_lines,
            fixed_lines,
            fromfile=f"a/{relative_file_path}",
            tofile=f"b/{relative_file_path}",
            lineterm="\n",
        )
        return "".join(diff)

    def save_patch(self, product_name: str, patch_name: str, diff_content: str) -> Path:
        """Saves a unified diff file flat under SAT_SALOME/products/patches/<patch_name>.

        Real `sat` (src/__init__.py: find_file_in_lpath) resolves a product's
        `patches` entries with a non-recursive `os.listdir()` directly under
        each PRODUCTPATH's `patches/` directory, so patches cannot live in
        per-product subdirectories or `sat` will never find them. The
        product name is prefixed onto the filename instead, to keep patches
        from different products from colliding in the flat directory.
        """
        self.patches_dir.mkdir(parents=True, exist_ok=True)

        if not patch_name.endswith(".patch"):
            patch_name = f"{patch_name}.patch"
        if not patch_name.startswith(f"{product_name}"):
            patch_name = f"{product_name}_{patch_name}"

        patch_file = self.patches_dir / patch_name
        patch_file.write_text(diff_content, encoding="utf-8")
        return patch_file

    def register_patch_in_pyconf(self, pyconf_mgr: PyconfManager, product_name: str, patch_name: str) -> None:
        """Updates product definition in pyconf to reference the new patch file."""
        if not patch_name.endswith(".patch"):
            patch_name = f"{patch_name}.patch"

        products = pyconf_mgr.config_dict.get("PRODUCTS", {})
        if product_name in products:
            prod_spec = products[product_name]
            patches = prod_spec.setdefault("patches", [])
            if patch_name not in patches:
                patches.append(patch_name)
            pyconf_mgr.save()

    def register_patch_in_product_file(self, product_path: Path, patch_name: str) -> None:
        """Registers a patch filename in a real product pyconf file's
        `default { patches : [...] }` list, by editing the file text
        directly.

        Real product files (e.g. SAT_SALOME/products/GEOM.pyconf, whether
        shipped or agent-generated via PyconfManager.write_product_file) are
        written in SAT's own pyconf DSL, not Python literal syntax --
        PyconfManager/exec() cannot load them, so registration here is a
        targeted text edit instead of a parse-modify-serialize round trip.
        """
        if not patch_name.endswith(".patch"):
            patch_name = f"{patch_name}.patch"

        content = product_path.read_text(encoding="utf-8")

        m = _PATCHES_LINE_RE.search(content)
        if m:
            existing = [e.strip().strip("'\"") for e in m.group(1).split(",") if e.strip()]
            if patch_name in existing:
                return
            existing.append(patch_name)
            new_list = ", ".join(f"'{e}'" for e in existing)
            content = content[: m.start()] + f"patches : [{new_list}]" + content[m.end():]
        else:
            block = _DEFAULT_BLOCK_RE.search(content)
            if not block:
                raise ValueError(f"Could not find a 'default : {{' block in {product_path}")
            insertion = f"\n    patches : ['{patch_name}']"
            content = content[: block.end()] + insertion + content[block.end():]

        product_path.write_text(content, encoding="utf-8")
