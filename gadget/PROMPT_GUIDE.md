# Local motion prompt harness · v1

Use the user's current local agent/model to write the final HYM motion description. No remote rewrite call is needed or permitted through Gadget. This guide is task guidance, not permission to generate extra jobs, buy APIs, or change robot settings.

## Workflow

1. Identify the requested operation: search, generate, retarget, or explicitly requested combination. Search and existing-motion retarget do not need prompt rewriting. Never generate or retarget implicitly.
2. Extract the action, actor's left/right, speed, direction, path/angle, rhythm, sequence, start/end posture, intentional contact, duration and count. Preserve specified values. Ask only about contradictions or missing details that materially change the action; otherwise use a simple natural movement without inventing angles, contacts or counts.
3. Write one single-person, observable motion description, 12–70 whitespace-separated English words, beginning exactly `A person `. Use a single paragraph of at most 1000 characters. Describe body movement, not camera, rendering, clothing, a story, instructions to a model, JSON or markdown. Prefer concise affirmative movement cues. Do not fill the word budget with repeated prohibitions.
4. Apply the full motion policy below selectively and concisely. Prefer roughly 25–55 words when adequate; never remove requested action details merely to reach this preference. Already suitable English needs no gratuitous rewrite, but still review its meaning against the policy.
5. Call `check_motion_prompt` (CLI: `check-prompt`) for format and duration validation, then perform the semantic checklist yourself. Format success is NOT semantic approval. Resolve contradictory requirements before submission. Do not silently split a long sequence into extra paid jobs; explain the duration limit and obtain the user's choice when needed.
6. Submit `generate_motion` with `prompt_mode=exact_english`, original_description and known rewrite_agent metadata, a stable request_id, and the authorized n/duration. Duration is a separate integer 1–120 seconds on Kimodo (1–12 on legacy HY-Motion-1.0, default 8); prose cannot set it. Original_description is provenance, not a second prompt. Unknown model names must be omitted. Never send local subscription credentials.
7. Poll at least 10 seconds apart. Inspect the returned effective prompt and available quality_report. Unknown or absent checks remain unverified. A rejected candidate may need a new prompt/seed and a new request_id linked with parent_request_id; transport retries reuse the original ID. Do not regenerate indefinitely or beyond the authorized count/budget.

## Motion policy

Generate physically plausible single-person motion suitable for humanoid retargeting.
Preserve the requested action, speed, direction, path, angle, rhythm and sequence.
For standing, marching, ordinary walking/running, stopping and turning, use a
comfortable hip-width stance and separate left/right foot tracks. Keep a visible
gap between the feet; avoid unintended scissoring, crossed ankles or feet touching.
During lateral steps do not bring both feet together. During turns use several
controlled steps rather than twisting one foot through the other.
Keep the hands and forearms visibly clear of the torso, pelvis and thighs.
Use modest shoulder abduction and softly bent elbows; swing arms forward/back
outside the hip outline, not through the body. Do not glue the hands to the hips.
Mention task-relevant foot separation and hand/body clearance concisely in the
English motion description. These are movement intentions, not verified distances.
Do not invent numerical clearances or claim collision-free/robot-safe results.
For intentional crossing steps or body contact in an explicitly requested dance
or gesture, preserve its meaning rather than silently replacing the action;
flag incompatibility when a custom request also requires strict non-contact.
Prompt wording cannot certify geometry, balance or dynamics. Only measured
robot-specific validation can establish clearance for a generated candidate.

Policy source: repository AGENTS.md bounded motion section; body_clearance_v1. This is a bundled snapshot, not a live geometry validator.

## Examples (adapt to the request; do not paste irrelevant constraints)

- Request: 向前慢走，然后停下。
  Prompt: A person walks slowly forward on separate hip-width foot tracks, swinging relaxed arms clear of the torso and thighs, then gradually slows to a stable stop with both feet apart.
- Request: 向右横移两步。
  Prompt: A person takes two controlled steps to their right, maintaining a visible gap between the feet without bringing them together, while keeping softly bent arms slightly away from the torso.
- Request: 原地向左转九十度。
  Prompt: A person turns ninety degrees to their left using several small controlled steps, keeping the feet on separate tracks and the hands clear of the hips, then settles into a stable stance.
- Request: 站着用右手挥手，左手不动。
  Prompt: A person stands with feet comfortably apart and waves their right hand beside the shoulder, keeping the left arm still slightly away from the torso and both forearms clear of the body.
- Request: 跳舞时右脚交叉到左脚前面。
  Prompt: A person performs a dance step by intentionally crossing the right foot in front of the left, then uncrosses into a comfortable stance, keeping the arms softly bent and clear of the torso.
  Review: Crossing is intentional; do not replace it with ordinary parallel walking. If strict non-contact is also requested and cannot be reconciled, clarify before submitting.
- Request: 双手拍胸，但手绝不能接触身体。
  Action: Ask whether chest contact or strict non-contact takes priority; do not fabricate a compromise and submit it silently.

Bad: "A person moves beautifully, cinematic, robot-safe and collision-free." This lacks concrete action, includes rendering words and makes unsupported safety claims.
Bad: Adding "exactly 8 cm clearance" or a wrist-lock change when the user never requested it. Do not invent numerical or retargeting constraints.

## Semantic checklist — local agent must review

- All requested action, laterality, speed, path, turn angle, rhythm, sequence and intentional contacts preserved?
- Relevant foot separation and arm/body clearance cues included without contradicting intentional contact?
- One feasible short clip for the requested duration, with no unauthorized added action, batch or retargeting?
- No invented clearance numbers, implicit wrist changes, camera/style filler, or safety certification?
- Conflicts resolved, original request retained, and format check passed?

Passing this checklist is a text review only. Generated geometry, balance, tracking and robot-specific collision clearance still require measured validation; do not label them passed from the prompt.
