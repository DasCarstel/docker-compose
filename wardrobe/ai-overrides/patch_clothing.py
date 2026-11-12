#!/usr/bin/env python3
"""Patch the cloaked clothing.py copy: allow ONE extra layer per body slot (PRO-347).

Kept as an ai-overrides bind-mounted file so the upstream image stays untouched.
"""
import sys

P = "/opt/docker-compose/wardrobe/ai-overrides/clothing.py"

src = open(P).read()

if "EXTRA_LAYER_TYPES" in src:
    print("already patched")
    sys.exit(0)

anchor = (
    'ROLE_SLOTS: dict[str, frozenset[str]] = {\n'
    '    "full_body": frozenset({"base_top", "bottom"}),\n'
    '    "suit": frozenset({"bottom", "suit"}),\n'
    '}\n'
)
extra = (
    '\n'
    '# Optional extra layers (PRO-347): a type listed here may take a second seat in an\n'
    '# already-filled single-item body slot, once per slot, when the first claimant is a\n'
    '# plain item of that same role. List order is wearing order, innermost first;\n'
    '# the prompt instructs the model accordingly.\n'
    'EXTRA_LAYER_TYPES: dict[str, frozenset[str]] = {\n'
    '    # a shirt worn open as a light overshirt over a lighter top\n'
    '    "base_top": frozenset({"shirt"}),\n'
    '    # a lighter outer layer worn under a heavier one\n'
    '    "outer_layer": frozenset(\n'
    '        {"jacket", "blazer", "coat", "overcoat", "parka", "poncho", "hoodie", "cardigan"}\n'
    '    ),\n'
    '}\n'
)
old_loop = (
    '    claimed: dict[str, UUID] = {}\n'
    '    kept: set[UUID] = set()\n'
    '    for iid in mandatory + multi_slot + single_slot:\n'
    '        item_type = item_type_map.get(iid, "")\n'
    '        slots = _slots_for_type(item_type)\n'
    '        taken = next((slot for slot in sorted(slots) if slot in claimed), None)\n'
    '        if taken:\n'
    '            logger.warning(\n'
    '                f"Removing {item_type} item {iid}: {taken} already filled by {claimed[taken]}"\n'
    '            )\n'
    '            continue\n'
    '        for slot in slots:\n'
    '            claimed[slot] = iid\n'
    '        kept.add(iid)\n'
    '    return [iid for iid in item_ids if iid in kept]\n'
)
new_loop = (
    '    claimed: dict[str, UUID] = {}\n'
    '    kept: set[UUID] = set()\n'
    '    for iid in mandatory + multi_slot + single_slot:\n'
    '        item_type = item_type_map.get(iid, "")\n'
    '        slots = _slots_for_type(item_type)\n'
    '        taken = next((slot for slot in sorted(slots) if slot in claimed), None)\n'
    '        if taken:\n'
    '            # Optional extra layer (PRO-347): one additional upper-body piece may\n'
    '            # join a filled slot — a shirt worn open as a light overshirt over a\n'
    '            # base top, or a lighter jacket under a heavier outer layer. Only the\n'
    '            # first extra per slot is kept and the slot must not be a multi-slot\n'
    '            # type, so the wearing order (list order, innermost first) stays clear.\n'
    '            layered_key = "layered:" + taken\n'
    '            first_type = item_type_map.get(claimed[taken], "")\n'
    '            if (\n'
    '                layered_key not in claimed\n'
    '                and len(slots) == 1\n'
    '                and ITEM_ROLE.get(first_type, "") == taken\n'
    '                and item_type in EXTRA_LAYER_TYPES.get(taken, frozenset())\n'
    '            ):\n'
    '                claimed[layered_key] = iid\n'
    '                kept.add(iid)\n'
    '                logger.info(\n'
    '                    f"Keeping {item_type} item {iid} as an extra layer over the {taken} item {claimed[taken]}"\n'
    '                )\n'
    '                continue\n'
    '            logger.warning(\n'
    '                f"Removing {item_type} item {iid}: {taken} already filled by {claimed[taken]}"\n'
    '            )\n'
    '            continue\n'
    '        for slot in slots:\n'
    '            claimed[slot] = iid\n'
    '        kept.add(iid)\n'
    '    return [iid for iid in item_ids if iid in kept]\n'
)

assert anchor in src, "ROLE_SLOTS anchor not found"
assert old_loop in src, "dedupe loop anchor not found"
src = src.replace(anchor, anchor + extra, 1).replace(old_loop, new_loop, 1)
open(P, "w").write(src)
print("patched OK")
