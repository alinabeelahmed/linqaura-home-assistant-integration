"""Writes custom_components/linqaura/translations/en.json from strings.json.

Custom integrations load only translations/<lang>.json, and Home Assistant does not resolve the
[%key:...%] references there, so they are filled in here from Home Assistant's own strings.
Run with a Home Assistant install available (e.g. inside the HA container or the test venv):
    python scripts/gen_translations.py
"""

import json
import re
import sys
from pathlib import Path

import homeassistant

HA = Path(homeassistant.__file__).parent
ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "linqaura"
REF = re.compile(r"\[%key:([^%]+)%\]")


def lookup(path: str) -> str:
    parts = path.split("::")
    if parts[0] == "common":
        data = json.loads((HA / "strings.json").read_text(encoding="utf-8"))["common"]
        parts = parts[1:]
    elif parts[0] == "component":
        data = json.loads((HA / "components" / parts[1] / "strings.json").read_text(encoding="utf-8"))
        parts = parts[2:]
    else:
        sys.exit(f"unknown reference {path}")
    for p in parts:
        data = data[p]
    return resolve(data) if isinstance(data, str) else data


def resolve(value):
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    if isinstance(value, str):
        return REF.sub(lambda m: lookup(m.group(1)), value)
    return value


strings = json.loads((ROOT / "strings.json").read_text(encoding="utf-8"))
out = ROOT / "translations" / "en.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(resolve(strings), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"wrote {out}")
