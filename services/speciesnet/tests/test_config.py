"""Tests for config.SpeciesNetSettings and the trigger filter."""

from __future__ import annotations

from settings import SpeciesNetSettings, from_yaml


class TestAppliesTo:
    def test_low_confidence_skipped(self) -> None:
        s = SpeciesNetSettings(min_confidence=0.5, trigger_classes=["bird"])
        assert s.applies_to("bird", 0.4) is False

    def test_above_min_confidence_passes(self) -> None:
        s = SpeciesNetSettings(min_confidence=0.4, trigger_classes=["bird"])
        assert s.applies_to("bird", 0.7) is True

    def test_empty_trigger_classes_matches_anything(self) -> None:
        s = SpeciesNetSettings(min_confidence=0.0, trigger_classes=[])
        assert s.applies_to("raccoon", 0.5) is True

    def test_substring_match(self) -> None:
        # `great_blue_heron` should match a config of ['heron']
        s = SpeciesNetSettings(min_confidence=0.0, trigger_classes=["heron"])
        assert s.applies_to("great_blue_heron", 0.9) is True

    def test_case_insensitive(self) -> None:
        s = SpeciesNetSettings(min_confidence=0.0, trigger_classes=["BIRD"])
        assert s.applies_to("bird", 0.9) is True

    def test_unrelated_class_skipped(self) -> None:
        s = SpeciesNetSettings(min_confidence=0.0, trigger_classes=["bird"])
        assert s.applies_to("raccoon", 0.9) is False


class TestFromYaml:
    def test_missing_section_returns_defaults(self) -> None:
        s = from_yaml({})
        assert s.enabled is False
        assert s.api_url == ""
        assert s.trigger_classes == ["bird"]

    def test_full_config(self) -> None:
        s = from_yaml({
            "speciesnet": {
                "enabled": True,
                "api_url": "https://api.example.com",
                "api_token": "secret",
                "min_confidence": 0.7,
                "trigger_classes": ["heron", "duck"],
                "max_concurrent": 8,
            },
        })
        assert s.enabled is True
        assert s.api_url == "https://api.example.com"
        assert s.api_token == "secret"
        assert s.min_confidence == 0.7
        assert s.trigger_classes == ["heron", "duck"]
        assert s.max_concurrent == 8

    def test_garbage_section_returns_defaults(self) -> None:
        s = from_yaml({"speciesnet": "not a dict"})
        assert s.enabled is False
