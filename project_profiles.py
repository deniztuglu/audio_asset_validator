"""Versioned project profiles, independent of the desktop UI."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from naming_rules import NamingPreset
from validator import Rules


@dataclass(frozen=True)
class ProjectProfile:
    name: str = "SFX Default"
    rules: Rules = Rules()
    naming: NamingPreset | None = NamingPreset()

    @classmethod
    def from_dict(cls, values: object, legacy_name: str = "Imported V1 profile") -> "ProjectProfile":
        if not isinstance(values, dict):
            raise ValueError("profile must be a JSON object")
        if "profile_version" not in values:
            rules = Rules.from_dict(values)
            naming = NamingPreset() if rules.naming_pattern == NamingPreset().pattern() else None
            return cls(legacy_name, rules, naming)
        if type(values["profile_version"]) is not int or values["profile_version"] != 1:
            raise ValueError("unsupported profile_version; this application supports version 1")
        if set(values) != {"profile_version", "name", "rules", "naming"}:
            raise ValueError("versioned profiles require profile_version, name, rules, and naming")
        name = values["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile name must not be empty")
        rules = Rules.from_dict(values["rules"])
        naming = None if values["naming"] is None else NamingPreset.from_dict(values["naming"])
        if naming is not None and naming.pattern() != rules.naming_pattern:
            raise ValueError("naming preset and naming_pattern disagree; use naming: null for a custom regex")
        return cls(name.strip(), rules, naming)

    @classmethod
    def load(cls, path: Path) -> "ProjectProfile":
        with path.open(encoding="utf-8") as source:
            return cls.from_dict(json.load(source), legacy_name=path.stem)

    def to_dict(self) -> dict:
        rules = asdict(self.rules)
        rules["bit_depths"] = list(self.rules.bit_depths)
        rules["channels"] = list(self.rules.channels)
        values = {"profile_version": 1, "name": self.name, "rules": rules,
                  "naming": self.naming.to_dict() if self.naming else None}
        # Apply the same validation when saving programmatically built objects.
        ProjectProfile.from_dict(values)
        return values

    def save(self, path: Path) -> None:
        values = self.to_dict()
        if path.suffix.lower() != ".json":
            raise ValueError("profile path must have a .json extension")
        with path.open("x", encoding="utf-8") as output:
            json.dump(values, output, indent=2, allow_nan=False)
            output.write("\n")
