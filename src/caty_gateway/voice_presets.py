"""Logical voice preset registry.

Append-only contract for shipped preset ids:
- never edit an existing preset row in place
- add a new versioned preset id for any remap or title change
"""

PRESETS = {
    "fish-neutral-ja-v1": {
        "provider": "fish",
        "reference_id": "0089dce5fefb4c6ba9b9f2f0debe1ddc",
        "display_name_ja": "おまかせ — 落ち着いた日本語",
    },
}


def get_preset(preset_id):
    preset = PRESETS.get(preset_id)
    if not isinstance(preset, dict):
        return None
    return dict(preset)
