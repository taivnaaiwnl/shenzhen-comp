"""Measured values for the video, through the project's own placeholder registry.

The video never types a measured number by hand: every value comes from
``docs/deliverables/placeholders.md`` and ``scripts/render_deliverables.py`` (same extraction, same
formats, same checks). For a local test run the renderer is pointed at a private evidence root under
``video/work/`` (the captured run's exported evidence plus the suite files), so nothing outside
``video/`` is written; for the Base Sepolia run it reads the repository's own ``evidence/``.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RENDERER = REPO / "scripts" / "render_deliverables.py"


def _load_renderer() -> Any:
    spec = importlib.util.spec_from_file_location("acb_render_deliverables_video", RENDERER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def build_local_root(
    dest: Path,
    network: str,
    evidence_dir: Path,
    attack_suite: Path | None,
    benign_suite: Path | None,
    deployment: Path | None = None,
) -> Path:
    """A private repository-shaped root holding only what the registry reads for a local run."""
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "evidence").mkdir(parents=True)
    shutil.copytree(evidence_dir, dest / "evidence" / network)
    if deployment is not None and Path(deployment).is_file():
        (dest / "deployments").mkdir()
        shutil.copyfile(deployment, dest / "deployments" / f"{network}.json")
    for src, name in ((attack_suite, "attack_suite.json"), (benign_suite, "benign_suite.json")):
        if src is not None and Path(src).is_file():
            shutil.copyfile(src, dest / "evidence" / name)
    for name in ("backtest.json", "dialogue_eval.json"):
        for src in (Path(evidence_dir) / name, Path(evidence_dir).parent / name):
            if src.is_file():
                shutil.copyfile(src, dest / "evidence" / name)
                break
    return dest


class Values:
    """Resolve ``{{name}}`` / ``{{name|filter}}`` exactly as the deliverables renderer does."""

    def __init__(self, network: str, root: Path | None = None):
        self.rd = _load_renderer()
        if root is not None:
            self.rd.REPO = Path(root)
        self.network = network
        self.entries, groups = self.rd.load_registry()
        self.ctx = self.rd.Context(network, groups)
        self.problems: list[str] = []

    def get(self, name: str, filt: str | None = None) -> str | None:
        entry = self.entries.get(name)
        if entry is None:
            self.problems.append(f"{name}: not in placeholders.md")
            return None
        try:
            return self.rd.apply_filter(self.ctx, self.rd.resolve(self.ctx, entry), filt, entry)
        except self.rd.Unresolved as exc:
            self.problems.append(f"{name}{'|' + filt if filt else ''}: {exc}")
            return None

    def render(self, text: str, label: str = "video") -> tuple[str, list[str]]:
        problems: list[str] = []
        out = self.rd.render_text(self.ctx, self.entries, text, label, problems)
        return out, problems

    def has_leftover(self, text: str) -> bool:
        return bool(self.rd.LEFTOVER_RE.search(text))

    def evidence(self) -> dict[str, Any]:
        return self.ctx.evidence()

    def path(self, rel: str) -> Path:
        return self.ctx.path(rel)
