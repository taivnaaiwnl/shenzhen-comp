"""Scorecard configuration (spec 4, 4.7, 13.3).

``configHash = chash({"scorecard": <config/scorecard.yaml as a dict>, "anchors": <the network's anchor
agentIds as decimal strings>})``. The combined object is what the archive file
``config/scorecard/archive/<configHash>.json`` stores (canonical JSON), so a posting made with an old
configuration can still be recomputed (:func:`find_config`).

:class:`ScorecardConfig` is the parsed, typed view the pure model reads. It is immutable and carries
its own ``config_hash``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from acb.canonical import atomic_write_bytes, canonical_bytes, chash
from acb.config import CONFIG_DIR, REPO_ROOT, load_yaml

SCORECARD_PATH = CONFIG_DIR / "scorecard.yaml"
ARCHIVE_DIR = CONFIG_DIR / "scorecard" / "archive"
STANDINGS = ("anchor", "A", "B", "NEW", "C", "NONE", "D")
FEATURE_IDS = ("F1", "F2", "F3", "F4a", "F4b", "F5a", "F5b", "F5c", "F6")
BIN_OPS = ("ge", "lt", "le")


class ConfigError(ValueError):
    """The scorecard configuration is malformed."""


@dataclass(frozen=True)
class PointsTable:
    """One row of the points table (spec 4.4)."""

    fid: str
    feature: str
    op: str
    bins: tuple[tuple[int, int], ...]
    otherwise: int
    max: int
    code: int
    code_alt: int | None = None

    def points(self, value: int) -> int:
        """Points of the first matching bin (thresholds inclusive as the op says), else ``otherwise``."""
        for threshold, pts in self.bins:
            if self.op == "ge" and value >= threshold:
                return pts
            if self.op == "lt" and value < threshold:
                return pts
            if self.op == "le" and value <= threshold:
                return pts
        return self.otherwise


@dataclass(frozen=True)
class ScorecardConfig:
    """Typed view of ``config/scorecard.yaml`` plus the network's anchors (spec 13.3)."""

    raw: Mapping[str, Any]
    anchors: tuple[int, ...]
    config_hash: str
    model_id: str
    detector_id: str
    profile: str
    a_min_age_s: int
    decay_start_s: int
    decay_h_s: int
    base: int
    clamp_lo: int
    clamp_hi: int
    band_a: int
    band_b: int
    band_c: int
    prior_weight_milli: int
    prior_bps: int
    late_credit_bps: int
    positive_quality: Mapping[str, int]
    adverse_weight: Mapping[str, int]
    good_standing_min: int
    min_order_amount: int
    ref_amount: int
    fail_cap_per_payer: int
    verified_weight: int
    spam_weight: int
    spam_min_invoices: int
    reporter_cap_milli: int
    hard_flag_inj_w: int
    cold_start_min_settled: int
    cold_d_fail_w: int
    cold_c_fail_w: int
    cold_c_flag: int
    cold_c_neg: int
    circular_max_hops: int
    circular_cap_bps: int
    spike_min_prior_orders: int
    spike_window_last: int
    tables: Mapping[str, PointsTable]
    mirror_enabled: bool
    mirror_revoke_previous: bool
    mirror_tag1: str
    repost_min_score_delta: int
    anchor_set: frozenset[int] = field(default_factory=frozenset)

    def combined(self) -> dict[str, Any]:
        """The object whose ``chash`` is ``configHash`` (and the content of the archive file)."""
        return combined_object(self.raw, self.anchors)

    def with_profile(self, profile: str) -> ScorecardConfig:
        """The same file with another ``active_profile`` (changes the hash)."""
        raw = dict(self.raw)
        raw["active_profile"] = profile
        return parse_config(raw, self.anchors)


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        raise ConfigError(f"{key}: expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise ConfigError(f"{key}: expected an integer, got {value!r}")


def _weights(data: Mapping[str, Any], key: str) -> dict[str, int]:
    table = data.get(key)
    if not isinstance(table, Mapping):
        raise ConfigError(f"{key}: expected a mapping")
    out = {str(k): _int(table, k) for k in table}
    missing = [s for s in STANDINGS if s not in out]
    if missing:
        raise ConfigError(f"{key}: missing standings {missing}")
    return out


def _table(fid: str, data: Any) -> PointsTable:
    if not isinstance(data, Mapping):
        raise ConfigError(f"points.{fid}: expected a mapping")
    op = data.get("op")
    if op not in BIN_OPS:
        raise ConfigError(f"points.{fid}.op must be one of {BIN_OPS}")
    bins = []
    for item in data.get("bins") or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ConfigError(f"points.{fid}.bins: each bin is [threshold, points]")
        bins.append((int(item[0]), int(item[1])))
    alt = data.get("code_alt")
    return PointsTable(
        fid=fid,
        feature=str(data["feature"]),
        op=op,
        bins=tuple(bins),
        otherwise=_int(data, "else"),
        max=_int(data, "max"),
        code=_int(data, "code"),
        code_alt=int(alt) if alt is not None else None,
    )


def combined_object(raw: Mapping[str, Any], anchors: Iterable[int | str]) -> dict[str, Any]:
    """``{"scorecard": raw, "anchors": [decimal strings]}`` (anchors as in ``deployments/<network>.json``)."""
    return {"scorecard": dict(raw), "anchors": [str(int(a)) for a in anchors]}


def config_hash(raw: Mapping[str, Any], anchors: Iterable[int | str]) -> str:
    """``chash`` of :func:`combined_object`."""
    return chash(combined_object(raw, anchors))


def parse_config(raw: Mapping[str, Any], anchors: Iterable[int | str]) -> ScorecardConfig:
    """Validate ``raw`` (the parsed YAML) and build the typed config."""
    if not isinstance(raw, Mapping):
        raise ConfigError("scorecard config must be a mapping")
    try:
        canonical_bytes(dict(raw))
    except TypeError as exc:  # floats or non-string keys
        raise ConfigError(f"scorecard config is not canonical-JSON safe: {exc}") from None
    anchor_ids = tuple(sorted(int(a) for a in anchors))
    profile = str(raw.get("active_profile") or "")
    profiles = raw.get("profiles") or {}
    if profile not in profiles:
        raise ConfigError(f"active_profile {profile!r} not in profiles")
    prof = profiles[profile]
    clamp = raw.get("clamp") or []
    if len(clamp) != 2:
        raise ConfigError("clamp must be [low, high]")
    bands = raw.get("bands") or {}
    prior = raw.get("prior") or {}
    cold = raw.get("cold_start") or {}
    circular = raw.get("circular") or {}
    spike = raw.get("spike") or {}
    mirror = raw.get("mirror") or {}
    points = raw.get("points") or {}
    tables = {}
    for fid in FEATURE_IDS:
        if fid not in points:
            raise ConfigError(f"points.{fid} missing")
        tables[fid] = _table(fid, points[fid])
    return ScorecardConfig(
        raw=dict(raw),
        anchors=anchor_ids,
        config_hash=config_hash(raw, anchor_ids),
        model_id=str(raw.get("model_id")),
        detector_id=str(raw.get("detector_id")),
        profile=profile,
        a_min_age_s=_int(prof, "a_min_age_s"),
        decay_start_s=_int(prof, "decay_start_s"),
        decay_h_s=_int(prof, "decay_h_s"),
        base=_int(raw, "base"),
        clamp_lo=int(clamp[0]),
        clamp_hi=int(clamp[1]),
        band_a=_int(bands, "A"),
        band_b=_int(bands, "B"),
        band_c=_int(bands, "C"),
        prior_weight_milli=_int(prior, "weight_milli"),
        prior_bps=_int(prior, "bps"),
        late_credit_bps=_int(raw, "late_credit_bps"),
        positive_quality=_weights(raw, "positive_quality"),
        adverse_weight=_weights(raw, "adverse_weight"),
        good_standing_min=_int(raw, "good_standing_min"),
        min_order_amount=_int(raw, "min_order_amount"),
        ref_amount=_int(raw, "ref_amount"),
        fail_cap_per_payer=_int(raw, "fail_cap_per_payer"),
        verified_weight=_int(raw, "verified_weight"),
        spam_weight=_int(raw, "spam_weight"),
        spam_min_invoices=_int(raw, "spam_min_invoices"),
        reporter_cap_milli=_int(raw, "reporter_cap_milli"),
        hard_flag_inj_w=_int(raw, "hard_flag_inj_w"),
        cold_start_min_settled=_int(raw, "cold_start_min_settled"),
        cold_d_fail_w=_int(cold, "d_fail_w"),
        cold_c_fail_w=_int(cold, "c_fail_w"),
        cold_c_flag=_int(cold, "c_flag"),
        cold_c_neg=_int(cold, "c_neg"),
        circular_max_hops=_int(circular, "max_hops"),
        circular_cap_bps=_int(circular, "cap_bps"),
        spike_min_prior_orders=_int(spike, "min_prior_orders"),
        spike_window_last=_int(spike, "window_last"),
        tables=tables,
        mirror_enabled=bool(mirror.get("enabled", True)),
        mirror_revoke_previous=bool(mirror.get("revoke_previous", True)),
        mirror_tag1=str(mirror.get("tag1") or "acb-grade"),
        repost_min_score_delta=_int(raw, "repost_min_score_delta"),
        anchor_set=frozenset(anchor_ids),
    )


def load_raw(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """``config/scorecard.yaml`` as a dict (BOM tolerant)."""
    data = load_yaml(path or SCORECARD_PATH)
    if not isinstance(data, dict):
        raise ConfigError("config/scorecard.yaml must be a mapping")
    return data


def load_config(anchors: Iterable[int | str] = (), path: str | os.PathLike[str] | None = None) -> ScorecardConfig:
    """The current scorecard config bound to ``anchors``."""
    return parse_config(load_raw(path), anchors)


def archive_dirs() -> tuple[Path, ...]:
    """Where archived configs are looked up: ``ACB_SCORECARD_ARCHIVE_DIR`` (if set), the repository
    archive ``config/scorecard/archive``, every exported ``evidence/<network>/scorecard/archive`` and
    ``$ACB_OUT_DIR/scorecard/archive``."""
    dirs: list[Path] = []
    override = os.environ.get("ACB_SCORECARD_ARCHIVE_DIR")
    if override:
        dirs.append(Path(override))
    dirs.append(ARCHIVE_DIR)
    evidence = REPO_ROOT / "evidence"
    if evidence.is_dir():
        dirs.extend(sorted(p for p in evidence.glob("*/scorecard/archive") if p.is_dir()))
    out = os.environ.get("ACB_OUT_DIR")
    if out:
        dirs.append(Path(out) / "scorecard" / "archive")
    return tuple(dirs)


def archive_config(cfg: ScorecardConfig, directory: str | os.PathLike[str]) -> Path:
    """Write the combined object to ``<directory>/<configHash>.json`` (canonical JSON) if missing."""
    target = Path(directory) / f"{cfg.config_hash}.json"
    if not target.is_file():
        atomic_write_bytes(target, canonical_bytes(cfg.combined()))
    return target


def find_config(config_hash_hex: str, current: ScorecardConfig | None = None) -> ScorecardConfig | None:
    """The config whose hash is ``config_hash_hex``: the current one, else an archived one (each archive
    file is checked against its hash). None when nothing matches (``CONFIG_NOT_FOUND``)."""
    wanted = config_hash_hex.lower()
    if current is not None and current.config_hash == wanted:
        return current
    for directory in archive_dirs():
        candidate = directory / f"{wanted}.json"
        if not candidate.is_file():
            continue
        try:
            obj = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(obj, dict) or chash(obj) != wanted:
            continue
        try:
            cfg = parse_config(obj.get("scorecard") or {}, obj.get("anchors") or [])
        except (ConfigError, KeyError, TypeError, ValueError):
            continue
        if cfg.config_hash == wanted:
            return cfg
    return None


__all__ = [
    "ARCHIVE_DIR",
    "FEATURE_IDS",
    "SCORECARD_PATH",
    "STANDINGS",
    "ConfigError",
    "PointsTable",
    "ScorecardConfig",
    "archive_config",
    "archive_dirs",
    "combined_object",
    "config_hash",
    "find_config",
    "load_config",
    "load_raw",
    "parse_config",
]
