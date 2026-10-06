# Vision — the factory as a full studio

Captured 2026-07-11 from Martin, verbatim in intent. This is the north star; the
ROADMAP is the ordered path toward it. Nothing here is committed work until it
reaches ROADMAP's Now/Next.

## The end state

A self-improving studio of AI agents that takes a project from idea to shipped,
end to end, with Martin as sponsor rather than operator.

**Two entry points, same agents:**
1. **Direct access** — Martin (or a manager agent) can address any single agent
   with a one-off task: "sound designer, make a 10-hour rain track". Every agent
   is independently invokable, not only reachable through the pipeline.
2. **Orchestrated** — a controller/manager agent receives a project + final
   objectives, decomposes it, delegates to worker agents through the factory,
   answers their results, and loops until the objectives are met (the local
   `/goal`). Manager agents may call worker agents themselves.

## Roles to staff (in order of real demand)

**Creative workers** — separate stacks, not personas (ADR-006 still rules: staff a
role only when a real consumer exists):
- **Sound designer / music composer** — MusicGen / Stable Audio Open. Consumers:
  YouTube relax channel (10 h ambient tracks = the passive-income pipeline),
  BlitzVolley game sounds.
- **Graphic designer** — Flux/SDXL via ComfyUI headless. Consumers: BlitzVolley
  sprites/assets, YouTube thumbnails and visuals.
- **Video editor (monteur)** — ffmpeg-driven assembly: image + 10 h soundtrack →
  rendered upload-ready video. Consumer: YouTube channel; later, more
  mainstream video formats once the trio (sound, image, editing) works.
- **3D modeler** — explicitly deferred. Useful someday, no consumer today.

**Judges** — the acceptance side, mirroring the workers:
- **Final client** — runs E2E tests (Playwright), sends feedback like a real user.
- **Visual judge, artistic judge, audio judge / sound engineer** — grade creative
  output against the acceptance criteria.
- **Testers** — the existing pytest/vitest lane.
- Any profession that is genuinely useful can become an agent; the test for
  staffing one stays "does it change output quality".

## The workflow (no room for vagueness)

- A **product owner / architect** role writes clear, precise acceptance criteria
  *before* work starts. That is the anti-infinite-loop device: agents challenge
  each other, but a hard, pre-agreed definition of done decides.
- Every project is fully structured: brainstorm → plan → roadmap → milestones,
  each step explicit and recorded.
- **End-of-project debrief, automatic, part of the workflow:** were the
  objectives met, revised up or down, and why. Output = concrete changes so the
  next project runs better (feeding CHANGELOG/DECISIONS/personas — the
  self-improving part).

## Standing constraints

- **Martin must always be able to take over manually** — no Claude credits must
  never mean a stalled factory. The operator CLI (`factory_cli.py`) is that
  guarantee; benchmarks must prove the local-only path works before relying
  on it.
- Cloud (Claude) = judgment: decomposition, review, arbitration. Local = labor.
  (Audit synthesis 2026-07; delegation v2 is the substrate.)
- Human gates stay: merge to master, spending money, publishing.

## Known risks (challenge log)

- **Agent judges of creative work are weak judges.** A 12 GB local model grading
  aesthetics drifts toward flattery. Mitigation: judges score against explicit
  written criteria (the PO artifact), produce comparative rankings of N variants
  rather than absolute verdicts, and Martin samples their calls until their
  agreement rate with him is measured. Trust is earned by benchmark, like models.
- **Process before product.** A full studio of roles is only worth building
  against real deliverables. Anchors: first monetizable YouTube video, first
  BlitzVolley asset batch. Staff roles as those pull them in.
- **Open-ended missions (find money ideas, societal problems) have no judge.**
  Run them as bounded ideation: N ideas, scored against explicit criteria,
  deduplicated against a persistent ledger, top 3 escalated to Martin. Never as
  an unbounded loop.
- **Throughput.** One 35b call ≈ 900 s on the 5070. Decomposition quality and,
  eventually, the 24 GB card (ROADMAP Later) are the levers.
