"""Friendly naming presets. Arbitrary expressions remain an explicit advanced mode."""

from dataclasses import dataclass
import re


PREFIXES = ("SFX", "DX", "FLY")  # Initial suggestions, not a fixed allowlist.
CASING = ("flexible", "uppercase", "lowercase")


def normalize_prefix(text: str) -> str:
    """Normalize editor input; stored profiles use the exact uppercase value."""
    value = text.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)*", value):
        raise ValueError("Use a prefix starting with a letter, with letters/numbers separated by _ or - (e.g. AMB or VO_EN).")
    return value


def _prefix_list(values: object) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError("select at least one naming prefix")
    for value in values:
        if not isinstance(value, str) or normalize_prefix(value) != value:
            raise ValueError("saved prefixes must be uppercase and contain no surrounding whitespace")
    if len(set(values)) != len(values):
        raise ValueError("naming prefixes must be unique")
    return tuple(values)


@dataclass(frozen=True)
class NamingPreset:
    prefixes: tuple[str, ...] = ("SFX",)
    capitalization: str = "flexible"
    variation_digits: int = 2
    available_prefixes: tuple[str, ...] | None = None

    @classmethod
    def from_dict(cls, values: object) -> "NamingPreset":
        required = {"prefixes", "capitalization", "variation_digits"}
        if (not isinstance(values, dict) or not required.issubset(values)
                or set(values) - required - {"available_prefixes"}):
            raise ValueError("naming must contain prefixes, capitalization, and variation_digits")
        prefixes = _prefix_list(values["prefixes"])
        available = None
        if "available_prefixes" in values:
            available = _prefix_list(values["available_prefixes"])
            if not set(prefixes).issubset(available):
                raise ValueError("available_prefixes must include every selected prefix")
        capitalization = values["capitalization"]
        if capitalization not in CASING:
            raise ValueError("capitalization must be flexible, uppercase, or lowercase")
        digits = values["variation_digits"]
        if type(digits) is not int or digits not in range(1, 5):
            raise ValueError("variation_digits must be an integer from 1 to 4")
        return cls(prefixes, capitalization, digits, available)

    def to_dict(self) -> dict:
        values = {"prefixes": list(self.prefixes), "capitalization": self.capitalization,
                  "variation_digits": self.variation_digits}
        if self.available_prefixes is not None:
            values["available_prefixes"] = list(self.available_prefixes)
        return values

    def pattern(self) -> str:
        # Validate even presets constructed directly by Python callers.
        NamingPreset.from_dict(self.to_dict())
        escaped = [re.escape(prefix) for prefix in self.prefixes]
        prefix = escaped[0] if len(escaped) == 1 else "(?:" + "|".join(escaped) + ")"
        word = {"flexible": "[A-Za-z0-9]", "uppercase": "[A-Z0-9]",
                "lowercase": "[a-z0-9]"}[self.capitalization]
        return rf"{prefix}_{word}+(?:_{word}+)*_[0-9]{{{self.variation_digits}}}\.[wW][aA][vV]"

    def example(self) -> str:
        word = {"flexible": "Footstep_Concrete", "uppercase": "FOOTSTEP_CONCRETE",
                "lowercase": "footstep_concrete"}[self.capitalization]
        return f"{self.prefixes[0]}_{word}_{1:0{self.variation_digits}d}.wav"
