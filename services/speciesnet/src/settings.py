"""Read-only access to the speciesnet section of scarguard.yml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SpeciesNetSettings:
    """In-memory snapshot of the ``speciesnet:`` config section.

    Defaults are conservative — service is opt-in via ``enabled: true``.
    """

    enabled: bool = False
    api_url: str = ""
    api_token: str = ""
    timeout_seconds: float = 15.0
    poll_interval_seconds: float = 3.0
    max_polls: int = 60
    bbox_padding_pct: float = 0.10
    min_confidence: float = 0.40
    trigger_classes: list[str] = field(default_factory=lambda: ["bird"])
    max_concurrent: int = 4

    def applies_to(self, class_name: str, confidence: float) -> bool:
        """Return True when a detection should be sent for classification.

        ``trigger_classes`` is empty → applies to every class.  Otherwise
        the class must be in the list (case-insensitive substring match
        so 'great_blue_heron' matches a config of ['heron', 'duck']).
        """
        if confidence < self.min_confidence:
            return False
        if not self.trigger_classes:
            return True
        target = class_name.lower()
        for entry in self.trigger_classes:
            ent = entry.lower()
            if ent == target or ent in target:
                return True
        return False


def from_yaml(cfg: dict) -> SpeciesNetSettings:
    """Extract a ``SpeciesNetSettings`` from a parsed scarguard.yml dict.

    Missing or malformed sections fall back to defaults — the service
    will simply log "disabled" and consume events without dispatching.
    """
    sec = cfg.get("speciesnet") or {}
    if not isinstance(sec, dict):
        logger.warning("speciesnet config section is not a mapping — using defaults")
        return SpeciesNetSettings()
    s = SpeciesNetSettings()
    s.enabled = bool(sec.get("enabled", False))
    s.api_url = str(sec.get("api_url", "") or "").strip()
    s.api_token = str(sec.get("api_token", "") or "")
    s.timeout_seconds = float(sec.get("timeout_seconds", s.timeout_seconds))
    s.poll_interval_seconds = float(sec.get("poll_interval_seconds", s.poll_interval_seconds))
    s.max_polls = int(sec.get("max_polls", s.max_polls))
    s.bbox_padding_pct = float(sec.get("bbox_padding_pct", s.bbox_padding_pct))
    s.min_confidence = float(sec.get("min_confidence", s.min_confidence))
    classes = sec.get("trigger_classes")
    if isinstance(classes, list) and classes:
        s.trigger_classes = [str(c) for c in classes]
    s.max_concurrent = max(1, int(sec.get("max_concurrent", s.max_concurrent)))
    return s
