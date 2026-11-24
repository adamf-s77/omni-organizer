"""Tier 1 deterministic + learned rule matching."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from models import DeterministicRule, LearnedRule, MonitoredScope, V3Settings
from scopes import is_under, top_level_subdir
from state_store import StateStore


@dataclass
class Tier1Match:
    tier: str  # "tier1_learned" | "tier1_rule" | "tier1_offline_fallback"
    dest_id: str
    rule_id: str
    reason: str


def _rule_matches(
    filename: str,
    ext: str,
    scope_id: str,
    *,
    scopes: list[str],
    extensions: list[str],
    regex: str | None,
    keywords: list[str],
) -> bool:
    if not (extensions or regex or keywords):
        return False  # never match-all
    if scopes and scope_id not in scopes:
        return False
    if extensions and ext not in {e.lower().lstrip(".") for e in extensions}:
        return False
    if regex:
        try:
            if not re.search(regex, filename):
                return False
        except re.error:
            return False
    if keywords and not any(k and k.lower() in filename.lower() for k in keywords):
        return False
    return True


def _allowed_in_zone(
    path: Path,
    dest_path: str,
    scope: MonitoredScope | None,
) -> bool:
    """Inside a sub-folder, never shuffle a file to another place in the SAME entry folder.

    e.g. Desktop/Work/report.pdf must not be pulled into Desktop/Work/_Archive,
    but an installer dropped there may be corrected to Downloads/Installers.
    """
    if scope is None:
        return True
    top = top_level_subdir(scope, path)
    if top is None:
        return True
    entry_dir = Path(scope.path) / top
    return not is_under(dest_path, entry_dir)


def match_rules(
    path: Path,
    scope_id: str,
    settings: V3Settings,
    rules: list[DeterministicRule],
    tier: str,
    scope: MonitoredScope | None = None,
) -> Tier1Match | None:
    filename, ext = path.name, path.suffix.lower().lstrip(".")
    for rule in rules:
        if not rule.enabled:
            continue
        dest = settings.get_destination(rule.dest_id)
        if dest is None:
            continue
        if not _rule_matches(
            filename, ext, scope_id,
            scopes=rule.scopes, extensions=rule.extensions,
            regex=rule.regex, keywords=rule.keywords,
        ):
            continue
        if not _allowed_in_zone(path, dest.path, scope):
            continue
        return Tier1Match(tier, rule.dest_id, rule.id, f"規則命中：{rule.name}")
    return None


def match_tier1(
    path: Path,
    scope_id: str,
    settings: V3Settings,
    store: StateStore,
    scope: MonitoredScope | None = None,
) -> Tier1Match | None:
    """Learned rules (user-confirmed) take priority over built-in deterministic rules."""
    filename, ext = path.name, path.suffix.lower().lstrip(".")
    learned: list[LearnedRule] = [r for r in store.list_learned_rules() if r.enabled]
    for rule in learned:
        dest = settings.get_destination(rule.dest_id)
        if dest is None:
            continue
        if not _rule_matches(
            filename, ext, scope_id,
            scopes=rule.scopes, extensions=rule.extensions,
            regex=rule.regex, keywords=[rule.keyword] if rule.keyword else [],
        ):
            continue
        if not _allowed_in_zone(path, dest.path, scope):
            continue
        label = rule.keyword or rule.regex or rule.id
        return Tier1Match("tier1_learned", rule.dest_id, rule.id, f"學習規則命中：{label}")

    return match_rules(path, scope_id, settings, settings.tier1_rules, "tier1_rule", scope)
