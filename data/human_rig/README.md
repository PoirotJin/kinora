# Kinora Human52

An independently specified, generic authoring skeleton. The rest offsets in
`g2h/human_rig.py` are explicit design dimensions, not extracted from a body
model or copied from an upstream skeleton. The 52-channel ordering preserves
interoperability with the existing motion protocol; it is not an SMPL body model.

The preview draws joints and bones procedurally. No third-party human mesh,
skinning weights or textures are required. Robot meshes retain their own sources.
Run `PYTHONPATH=. python -c 'from g2h.human_rig import write_assets; write_assets()'`
to generate binary and JSON preview descriptors. `provenance.json` records hashes.

New motions carry their rest joints and hierarchy. Older motion files retain their
own geometry: editing reconstructs and checks the rest rig from the file's stored
rotations and joint positions. Inconsistent imports are rejected, not relabelled.
