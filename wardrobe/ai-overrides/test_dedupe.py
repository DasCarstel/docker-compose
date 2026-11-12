#!/usr/bin/env python3
"""Synthetic dedupe test for the EXTRA_LAYER_TYPES patch (PRO-347)."""
import sys

sys.path.insert(0, "/app")

from uuid import uuid4

from app.utils.clothing import deduplicate_by_body_slot

ids = [uuid4() for _ in range(6)]
tee, shirt, hoodie, jacket, coat, pants = ids
name = {
    tee: "t-shirt",
    shirt: "shirt",
    hoodie: "hoodie",
    jacket: "jacket",
    coat: "coat",
    pants: "pants",
}
m = lambda lst: [name[i] for i in lst]

print("overshirt tee+shirt      :", m(deduplicate_by_body_slot([tee, shirt, jacket], {**name, "jacket": "jacket"})))
print("double outer tee+jacket+coat:", m(deduplicate_by_body_slot([tee, jacket, coat, pants], name)))
print("wrong order shirt first  :", m(deduplicate_by_body_slot([shirt, tee, pants], name)))
print("plain unchanged          :", m(deduplicate_by_body_slot([tee, pants], name)))
print("three-stack tee+shirt+jacket+coat :", m(deduplicate_by_body_slot([tee, shirt, hoodie, jacket, coat, pants], name)))
