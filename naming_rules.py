"""Friendly naming presets. Arbitrary expressions remain an explicit advanced mode."""

from dataclasses import dataclass


PREFIXES = ("SFX", "DX", "FLY")
CASING = ("flexible", "uppercase", "lowercase")


@dataclass(frozen=True)
class NamingPreset:
    prefixes: tuple[str, ...] = ("SFX",)
    capitalization: str = "flexible"
    variation_digits: int = 2

    @classmethod
    def from_dict(cls, values: object) -> "NamingPreset":
        if not isinstance(values, dict) or set(values) != {
            "prefixes", "capitalization", "variation_digits"
        }:
            raise ValueError("naming must contain prefixes, capitalization, and variation_digits")
        prefixes = values["prefixes"]
        if (not isinstance(prefixes, list) or not prefixes
                or any(prefix not in PREFIXES for prefix in prefixes)
                or len(set(prefixes)) != len(prefixes)):
            raise ValueError("select one or more unique naming prefixes: SFX, DX, FLY")
        capitalization = values["capitalization"]
        if capitalization not in CASING:
            raise ValueError("capitalization must be flexible, uppercase, or lowercase")
        digits = values["variation_digits"]
        if type(digits) is not int or digits not in range(1, 5):
            raise ValueError("variation_digits must be an integer from 1 to 4")
        return cls(tuple(prefixes), capitalization, digits)

    def to_dict(self) -> dict:
        return {"prefixes": list(self.prefixes), "capitalization": self.capitalization,
                "variation_digits": self.variation_digits}

    def pattern(self) -> str:
        # Validate even presets constructed directly by Python callers.
        NamingPreset.from_dict(self.to_dict())
        prefix = self.prefixes[0] if len(self.prefixes) == 1 else "(?:" + "|".join(self.prefixes) + ")"
        word = {"flexible": "[A-Za-z0-9]", "uppercase": "[A-Z0-9]",
                "lowercase": "[a-z0-9]"}[self.capitalization]
        return rf"{prefix}_{word}+(?:_{word}+)*_[0-9]{{{self.variation_digits}}}\.[wW][aA][vV]"

    def example(self) -> str:
        word = {"flexible": "Footstep_Concrete", "uppercase": "FOOTSTEP_CONCRETE",
                "lowercase": "footstep_concrete"}[self.capitalization]
        return f"{self.prefixes[0]}_{word}_{1:0{self.variation_digits}d}.wav"
