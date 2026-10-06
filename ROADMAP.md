# Roadmap — improvement backlog

The self-improvement TODO. Ordered roughly by leverage. Move items to CHANGELOG
when shipped.

North star: `docs/vision.md` (full studio — creative workers, agent judges, PO-owned
acceptance criteria, automatic debriefs). Priorities below serve it.

**Operator direction, 2026-07-22 (Martin) — the order of the next phases:**
1. **A local chat/agent that is flawless and fast.** The "Chat / agent lane"
   section below is the active worklist; everything else waits behind it.
2. *Then* heavier benchmarks and real projects, on that stable base.
3. *Then* loops + self-improvement (lessons auto-tuning, the continuous loop).
The jobs/ladder lane is mature enough to be maintenance-only meanwhile: it
runs 3/3 green on the multi-file rig and has a 12-job regression suite.

## Now
- [ ] **Chat/agent efficiency campaign** — agreed with Martin 2026-07-23, five
      blocks, in this order. Each gets its own spec → plan → implementation.
      - **B — token budget** (+ the prompt-assets half of E). Spec:
        `docs/design/specs/2026-07-23-chat-agent-token-budget-design.md`.
        Bench rig `experiments/agent_bench/`, seven line items measured off the
        server's real counters (`prompt_n` + `cache_n`), not chars/3 estimates.
        **Line items 1, 2, 6 MEASURED 2026-07-23**
        (`experiments/results/20260723_prompt_budget.md`, registry rows filed):
        prompt decomposition, tool-schema cost (web toolset = 2.9× native per
        schema, doubles every turn for zero outcome gain), thinking share
        (empty on the 30b). Prefix byte-stability SHIPPED. Two harness bugs
        surfaced and filed: MCP server locks the workspace CWD, dead MCP server
        silently shortens the KV prefix. **Line items 4 and 5 MEASURED
        2026-07-23** (`20260723_compaction_cache.md`, `20260723_eviction_trial.md`):
        reaching the 64k compaction threshold needed a read-gated `huge_chain`
        task — three transform tasks failed because the 30b scripts, quits, or
        blind-edits rather than read. `--cache-reuse 256` FALSIFIED outright (no
        effect on the compaction form either); tool-output eviction KEPT (off is
        ~43 % slower, it pays for itself), `EVICT_KEEP=12` unchanged. **Block B
        is done** bar the web-toolset **cut/curation** decision, which the
        numbers tee up (2179 tokens, zero outcome gain) but which is a product
        call. The last-call metric is noisy; a per-call prompt instrument is
        filed if either lever wants a precise number.
      - **D — end-to-end audit** of every chat and agent workflow (Playwright on
        the studio + CLI). Absorbs the "lot UX/bugs studio" already filed
        À TRIER in the optimisation registry. Produces bugs, not optimisations.
        The two bugs B's bench had found here are FIXED 2026-08-06 without
        waiting for the audit: **the MCP server that kept the workspace locked**
        by its CWD (`9e4cbcd` — `Factory.shutdown()` closes the registry, and
        the studio and the stdio server both call it) and **the dead MCP server
        that shortened the tool list in silence** (`2ecd3da` — the last known
        schemas hold the KV prefix). Registry rows updated.
      - **A — speed**: 128k unbenched, keep-alive/swap uncosted, MTP on the chat
        lane, ubatch.
      - **C — quality/completion**: needs a corpus and a judge. Most expensive,
        most fragile (two measurement bugs on 07-23).
      - **E — the learning loop**: automatic post-session analysis and continuous
        optimisation. Needs B's metrics to know what to optimise. Its other half
        (what goes into the prompt) is merged into B.
- [x] **Stale llama-server hijacks the port** — SHIPPED 2026-07-22 (`ce1d816`).
      Found by accident while measuring the turn work: the operator's box had
      two llama-servers and "everything freezes when two agents run". Both were
      configured for port 8091 and only the orphan owned it. A studio that dies
      without `shutdown()` strands its server (children survive their parent on
      Windows); `ensure()` then spawns a child that cannot bind, and since
      `health_fn` only asks the URL, the ghost answers `/health` and the
      manager believes its own child is serving — so every request ran on the
      orphan's ctx/KV, silently defeating the per-lane tuning, while the
      stranded child held ~8.8 GB of VRAM for nothing.
      `ensure()` now reclaims the port before spawning, and only from a process
      whose image is `llama-server` (own leftovers, not a port war); the studio
      unloads its runtime in a `finally` for clean exits. Verified live: crash
      the studio → orphan holding 6956 MiB; the next studio logs
      `llama-server 16492 squats port 8091: reclaiming it` and exactly one
      server remains.
      Lesson worth keeping: **a health check on a URL does not prove the
      process you spawned is the one answering.**
- [x] **Job regression suite** — SHIPPED 2026-07-18: `harness/regress.py` +
      `regress check/run/baseline` CLI, corpus of 12 frozen jobs replayed at
      their archived base_sha through the real loop_job, baselines seeded from
      archived results, confirmation replays (red 2-of-3), report.md/json per
      run. Spec: docs/design/specs/2026-07-18-regression-suite-design.md.
- [x] **Diff-mode re-run measured** — DONE 2026-07-17 evening (audit
      20260717-crgpd-rerun-diff): start-stage 3 confirmed (12 min vs 27.5),
      but SEARCH/REPLACE landed zero edits (AnchorNotFound ×2 on 30b,
      16k-token reply at the cap) — worse than whole mode's 6/8. Quant A/B
      (j_b0b7fd13, unsloth UD-Q4_K_XL): identical failure — quant falsified
      as the lever; likeliest cause = model "fixes" the accent-stripped
      French comments while copying anchors. Follow-ups below.
- [x] **Anchor-matching tiers** — SHIPPED (`harness/patch.py:130-233`): exact
      (substring) → trailing-whitespace → indent → unicode/accent, whole-line
      for the loose tiers, ambiguity refused, matched tier recorded per edit.
      MEASURED on the crgpd rig (audit 20260718-anchor-tiers-rerun): the
      spec's whole history is whole 0/1 → diff exact-only 0/2 → tiers 1/2 →
      **3/3 green** (413/500/134 s) once the caps + temp below landed; run 2
      was closed by the 35b on an *indent*-tier anchor — first live loose-tier
      firing, an edit yesterday's harness would have discarded.
- [x] **Persist raw model replies** — SHIPPED (`harness/job_store.py:192`):
      `jobs/j_*/attempt_N.txt`. Both anchor post-mortems had stalled on "we
      can't see what the model actually wrote"; the tier work above is what it
      unblocked.
- [x] **Diff-mode prompt caps** — SHIPPED (`harness/loop_job.py:131`): "keep
      each SEARCH under 15 lines, at most 8 edits". Paired with
      `diff_temperature` 0.3 on the 30b (`loop_job.py:50`, applied at `:243`),
      which is what actually changed the reply *shape*: 1.7-2.6k-token replies
      that apply, instead of the 10-16k bonfires that never once landed.
- [x] **Operator CLI** — DONE 2026-07-11 (`harness/factory_cli.py`): delegate/status/
      result/log/cancel from a terminal, `result --save-diff` for a manual git apply.
- [x] **Reviewer gate in loop_job** — DONE 2026-07-11, advisory v1 (ADR-016): the
      Reviewer persona judges the diff of every green job, verdict in `result.json`.
      Promote to blocking once its agreement rate with Martin/Claude is measured.
- [ ] **First benchmark baseline** — RE-AIM before running (2026-07-22): the
      original spec (roman + calculator × {7b, 14b, 30b} × 3 on Ollama) is
      stale on both axes. Runtime is llama-server now, and the ladder is
      7b → 30b → 35b-MTP: the 14b is a *removal* candidate (item below), not a
      rung to benchmark. The swap-cost question it was meant to answer is also
      half-answered by the job regression suite (12 real jobs) and the
      llama-server bench. Keep it only as "seeded-task baseline for the
      current ladder", and run it after the chat/agent lane is stable.
- [ ] **Reviewer agreement benchmark** — label real diffs (Martin/Claude verdicts)
      vs the advisory reviewer's; the promotion criterion of ADR-016. First 2
      labels (hybrid session 1): 1 false REJECT, 1 correct ACCEPT — 1/2.
      Label #3 (j_1e75691f, vitest smoke): REJECT on a functionally correct
      diff with real minor scope creep (cn got a return type + semicolons) —
      half-credit at best. Label #4 (j_5a1e2ee8, session-audit): correct
      ACCEPT, and it caught the max−min duration deviation and judged it
      benign — first genuinely useful reviewer note. Label #5 (j_af40da82,
      placement module, 2026-07-25): **false REJECT, the worst one yet** — it
      claimed a missing closing quote in the IIFE tail, on a file that had just
      passed 12 spec tests and a 219-test regression (node cannot run what it
      cannot parse) and that `node --check` parses clean. It invented a defect
      its own evidence disproved. Running tally: ~2.5/5. Two conclusions: it
      stays advisory, and its prompt should carry the test results it judges.

- [x] **ChatGPT web lane — proved live, and what the proof cost** (2026-07-29,
      closed 2026-07-30 by a green `selftest` in a single run).
      Provable by script, no studio and no model in the loop:
      `chatgpt_web.py login | check | projects | use-project | ask | selftest`
      (`selftest` exits 0/1 on two probes: a nonce echo, and a tool call read
      back by `llama_client.parse_text_tool_calls`).
      - **PROVED LIVE**: the transport (nonce round-trip, repeatedly), and the
        agent lane's tool call — GPT-5.2 emitting `<function=read_file>` through
        `render_prompt`'s own tools path, recovered by the loop's parser.
      - **Three stacked faults the unit tests could not see.** Reading was a
        `window.fetch` patch, and the app issues the answer POST **from a
        worker** — an init script never reaches that realm, so the tee only ever
        saw page-load JSON. It also matched the word "conversation" in the URL,
        so `/conversation/init` and `/conversations?offset=` ended the turn with
        an empty answer in 1.4 s **and reported success**. And Playwright's sync
        API delivers events only inside a Playwright call, so blocking on
        `queue.get()` starved its own queue. Reading is now CDP
        (`Network.streamResourceContent`) selected on
        `content-type: text/event-stream`, and the reader ticks.
      - **A stream that starts before the send is not this turn's answer**: the
        app replays the previous conversation on its own stream
        (`resume_conversation_token`), which handed the echo probe the tool call
        from the turn before it. The reader is armed at send.
      - **The protocol's wording is load-bearing.** Offered `read_file`,
        GPT-5.2 refuses ("I have no access to a read_file tool") — truthfully,
        from where it stands. What flips it: saying whose pipeline this is, that
        the block is a REQUEST not an action, and carrying it under
        `[instructions]`. What breaks it again: "output the block and nothing
        else". Also: an ask that says "the tool available to you" invites the
        capability check and gets refused — name the tool.
      - **Google will not sign in inside an automated browser.** The lane runs
        the operator's real Chrome (`channel="chrome"`), drops
        `--enable-automation` and disables `AutomationControlled`.
      - **The account is on the FREE plan.** The wall is real
        (`modal-conversation-history-rate-limit`, met repeatedly on 2026-07-29),
        but its budget is NOT the "~1-2 messages" this entry first claimed:
        2026-07-30 spent nine messages across three runs without meeting it
        once. What is established is the wall's existence and its handling, not
        its size — the lane names it instead of retrying a click into it, a late
        one (the quota running out on the message *before*) is caught on the
        composer click rather than blamed on the composer, and `selftest` grades
        it as its own verdict (exit 2) so "the account refused" is never read as
        "the lane is broken". A paid plan remains what makes the premise ("spend
        a subscription, not tokens") true at working volume.
      - **All three probes green in one run, 2026-07-30**: `selftest` exit 0 —
        transport 6.0 s (`PONG-fd7056`), agent lane 5.3 s
        (`read_file({'path': 'notes.txt'})`), loop 6.7 s (`CARROT-45e748`).
        The third probe is the half the others could not see: shown its own call
        and the `[result of read_file]` the harness pasted back, the model uses
        the result and does not call again — an agent that re-called would spend
        a message per iteration and never converge. Stateless rendering makes
        that whole exchange cost one message.
      - Conversations are filed in the account's `GPT_code` project
        (`use-project`); the pin lives beside the profile in
        `cache/chatgpt-project`, gitignored, because it belongs to the signed-in
        account rather than to the repo.
      - **And through the studio, not only by script** (2026-07-30, session
        `c_9efc9113`): an agent session in `auto` mode on `@chatgpt/gpt-5.2`,
        workspace `GPT_code`, asked for the README's title. The lane emitted
        `<function=read_file>`, `factory_mcp` executed it against the real
        workspace, pasted the result back, and the model answered `GPT_code`.
        The scripted proofs and the studio agree, so `_transport`'s browser
        branch is proved where it is actually used.
      - Known gap, not a bug: the turn reports `eval_count 0`, `0 tokens/s`,
        `cost 0` — the web app bills a subscription and announces no usage, so
        the studio's counters read zero on this lane. Time is measurable
        (`ttft_s 3.7`), volume is not.
      - **The wall was measured, and it is not the wall we described**
        (2026-07-30, ~22 model turns between 09:38 and 09:56, then met): it says
        *"Trop de requêtes — vous envoyez des demandes trop rapidement… veuillez
        attendre quelques minutes"*. That is a **PACE** limit, not the message
        allowance the harness was asserting — and a paid plan raises an
        allowance, not necessarily a pace. Two walls arrive behind one marker;
        the message now quotes the site and stops explaining on its behalf
        (`selftest` exit 2 against the standing wall, verdict proved live).
      - **How long it holds: not answered, and the honest reading says why.**
        Seven minute-spaced probes after 09:56 all read WALL; the eighth, at
        10:12:56 (~17 min), read CLEAR. One `ask` 40 s later was refused, and
        five probes 15 s apart after that were WALL again. So either the probe
        gave a false CLEAR (the modal is known to arrive late) or the reset is
        **probationary** — one message re-arms it. Those two are told apart by
        knowing WHERE the modal was seen, which the code could not say: both
        readings produced the same sentence. It now names the place (already on
        the blank chat = the account is refusing before we typed; only at send
        time = this message crossed the line), so the next occurrence decides it
        without another morning. Meanwhile `check` is not a reset detector, and
        nothing in the harness should treat it as one.
      - **"A new chat lifts it" — falsified twice, for free** (the wall is read
        off a page load, so asking costs no message). By construction the lane
        ALREADY opens a fresh conversation for every turn (`_ask` →
        `_open_new_chat`, stateless rendering), so all the walled turns were new
        chats. And the same modal appears on the main chat list as inside the
        `GPT_code` folder, so it is not folder-scoped either: it is the account,
        despite the modal's own word "conversations". Waiting is what clears it —
        which is why the turn fails cleanly rather than retrying (Martin, same
        day: fail cleanly, transcript kept, resend later).
      - **Usage rule for agent tasks on this lane, measured both ways.** The
        same five-step read-only task was refused ("les outils de lecture de
        fichiers ne sont pas disponibles pour moi ici") when it opened with a
        prohibition and named no tool, and **executed four calls plus a final
        answer** when the tools were named (session `c_eae74d2b`, 5 turns,
        32 s, 0 errors). The rule that saved the protocol yesterday — *name the
        tool* — is a rule for the TASKS too, not only for the probes: an
        instruction about what the model may not do invites the capability check
        that the protocol exists to avoid.
      - **Two defects the wall exposed, both fixed.** `selftest` died in `print`
        while quoting the modal (a French narrow no-break space on a cp1252
        console): a run that had already decided exit 2 exited 1 with a
        traceback, so the CLI puts both streams on UTF-8 before saying anything.
        And with the studio holding the lane, the CLI reported
        `TargetClosedError` under sixty lines of Chrome flags — a Chrome profile
        has ONE owner, and the blanket `except` around the "no Chrome installed"
        fallback was reporting the second failure instead of the first. There is
        now a filesystem probe (`lockfile` present and unopenable = a live owner,
        present and writable = a crash's debris) and the fallback only catches a
        missing browser.
      - Known cosmetic: killing the context at the end of a run can leave the
        Playwright node driver writing to a closed pipe (`EPIPE` stack on
        stderr). The verdict is the exit status, and it is unaffected.

## Next
- [x] **/goal v1 — the controller** — SHIPPED 2026-07-17 as the contract +
      durable bookkeeping (not an autonomous loop, on purpose): `goal_store.py`
      (goals/g_*/: goal.md brief, state.json backlog, debrief.md) + `goal`
      CLI verbs (new/add/item/status/show/done/abandon/list). Guards: project
      must be in factory.toml, item statuses validated, `done` refuses work in
      flight, debrief is part of finish. Docs: docs/goal.md + spec
      2026-07-17-goal-v1-design.md. Still open for v2: MCP tools for a
      scheduled manager, pilot on a real multi-item deliverable.
- [x] **vitest runner** — DONE 2026-07-14 (`harness/vitest_runner.py`), and
      validated the same evening by a REAL lucena delegation: j_1e75691f,
      formatEloDelta in src/lib/utils.ts, qwen2.5-coder:7b first rung, 2
      attempts, 14.8 s, spec 4/4, lucena regression green, junctions detached,
      lucena untouched. Judge outside the tree (ADR-010): harness-owned
      dependency-free config, node_modules junctioned into worktree+tests dir
      and detached before teardown. `runner = "vitest"` in factory.toml is all
      a Next.js project needs (lucena added). Still open: the E2E-client judge
      lane (Playwright) for web projects.
- [x] **Prefill + TTFT metrics** — DONE 2026-07-14, and shipped BY the local
      ladder (hybrid session 1, jobs j_9631eba1 + j_0e2bf531, both 7b first
      attempt): loop.py attempt records + benchmark summarize()/to_markdown()
      carry prefill tok/s and ttft_s. First data: 7b ≈ 4800 prefill tok/s,
      TTFT 3-4 s on this box.
- [x] **Ollama FA + KV q8 env flags** — FALSIFIED 2026-07-15: identical tok/s
      within noise at 32k and 64k (bottleneck = CPU-offloaded MoE experts).
      Side-finding: 64k ctx only costs ~7 % gen speed vs 32k. Details and
      measurement traps in `experiments/results/20260715_ollama_fa_kvq8_bench.md`.
- [x] **llama-server bench vs Ollama** — MEASURED 2026-07-18, CONFIRMED (not
      falsified): `experiments/results/20260718_llamaserver_bench.md`. Ollama
      hides `--n-cpu-moe` and `--ubatch`; tuned llama.cpp wins on both models —
      30b +30 % gen / +48 % prefill, 35b +42 % gen with `--spec-type draft-mtp`
      (draft acceptance 88 % on code prompts). The `--n-cpu-moe` cliff is
      brutal (one notch too low = weights spill to managed memory, ÷2.5 on
      everything), so the retained configs keep one notch of margin. Gains are
      LIVE in `factory.toml` (`[llama_server.models]`: 30b ncmoe 24 — 20 is the
      bench optimum at short ctx but overflows at 32k KV; 35b ncmoe 26 + MTP).
      Two bench traps found by autopsy first: `--parallel 4` silently quarters
      the context, and without sampling-profile passthrough llama.cpp's
      defaults re-drown the diff replies (fixed `b066c1b`).
- [x] **Prompt-cache stability** — MEASURED 2026-07-22, and it splits in two.
      Full write-up: `experiments/results/20260722_ttft_cache_reuse.md`, rig
      `experiments/ttft_cache_reuse_bench.py` (chat lane: 30b, ctx 64k, KV q4).
      (a) **The byte-stable prefix is CONFIRMED and worth more than expected**:
      a stable prefix evaluates 17 tokens per turn, a mutated one ~3170 —
      **~11x on TTFT, 0.10 s vs 1.13 s**. That is the justification for a
      constraint already being paid: lessons (`0a277d6`) and workspace
      MEMORY.md (`8121bac`) snapshot their block once per session precisely to
      keep this prefix identical. Any future "append to the system prompt
      mid-session" idea now has a price tag: ~1 s per turn.
      (b) **`--cache-reuse 256` is FALSIFIED** on the shapes tested: identical
      `prompt_n` to the token and identical TTFT with the flag on and off. The
      gain in (a) comes from llama-server's native longest-common-prefix slot
      caching — this item conflated the two. Flag kept at zero measured cost
      (falsified ≠ deleted). **Untested and worth doing: reuse past a
      mid-prompt divergence**, which is exactly what chat compaction produces
      when it evicts old tool outputs — the shape the flag was designed for.
      With this, every measurement from the llama-server migration is closed.
- [ ] **Task generator** — Client → Spec Writer → Tester pipeline that mints new
      seeded tasks automatically, growing the benchmark suite.
- [ ] **Model preload / keep-alive** — set `OLLAMA_KEEP_ALIVE` and order runs to
      minimize 18 GB model swaps; measure latency saved.
- [ ] **Context sweep** — benchmark architect at 32k/64k/128k; find where RAM spill
      kills throughput on 12 GB. Right-size the num_ctx claims.
- [x] **Multi-file tasks** — RUN 2026-07-17 (j_c32e89fd, conformergpd, 3 targets
      ~900 lines): mechanically sound (validation, worktree, judge, regression
      all held), capability-bound — peak 6/8 on 30b, 35b hit the length wall
      mid-thinking before any code. Full autopsy:
      audits/20260717-conformergpd-pilot.md. Follow-ups below.
- [ ] **Diff-based edits for the ladder** — whole-file re-emission is the
      binding constraint on multi-file jobs (~7-9k tokens of code per attempt
      before any reasoning). Let the model emit unified diffs or per-file
      blocks; the 35b's thinking budget then fits. (conformergpd pilot, #2)
- [ ] **Persist the best attempt's diff on failure** — a failed job discards
      its tree; the 30b's 6/8 near-miss was unrecoverable. Save the
      best-scoring attempt's diff in result.json so the manager can finish by
      hand. (conformergpd pilot, #4)
- [ ] **Question the 14b rung** — dense, 22.6 tok/s on this box, burned 11.5
      of the pilot's 27.5 minutes for 0/8, and has never closed anything the
      7b couldn't. Candidate: drop it or replace with a small MoE. (pilot, #3)
- [ ] **Per-project venv** — isolate deps from the global Python 3.9.

## Chat / agent lane (operator feedback 2026-07-15, + notes Martin 2026-07-18)
- [x] **Chat/studio → llama-server** — CODE SHIPPED 2026-07-19
      (`77c3de0`..`61307ba`): zero Ollama in the codebase. Chat/agent ride
      `llama_client.chat_stream` (SSE, tool calls, thinking deltas); lifecycle
      is "lock = VRAM" (the GPU lock is taken on load and released on an idle
      timer, one server per model); the Modèles tab reads the
      `[llama_server.models]` catalogue; 596 tests green. STUDIO SMOKE LIVE OK
      (2026-07-19, Playwright): chat 7b 112 tok/s then 30b 26 tok/s, both at
      ctx x/65536; unload from the Modèles tab freed VRAM 11.8->0.9 GB. ctx
      risk CLEARED per-lane (`d3f6125`): on 12 GB, q8 KV at 64k spills to
      system RAM (WDDM caps dedicated VRAM) and halves the 30b (measured 31 vs
      59 tok/s in q4). So the CHAT lane runs 64k with q4 KV (~59 tok/s 30b, ~51
      35b-MTP) and the JOBS lane keeps its tuned 32k q8 (66 tok/s, diff
      precision) -- factory.toml `ctx` is the jobs default, factory_mcp
      overrides ctx+KV for chat; the chat budget is clamped to CHAT_NUM_CTX.
      JOB-REGRESSION PASSED (run `20260719-125546`, full tier 12 jobs): zero
      correctness regression, efficiency net-positive (3 IMPROVED, 2 PROGRESS,
      4 OK, 2 DEGRADED-but-green); the lone "REGRESSION" verdict is the known
      flaky ranks.js `j_3a6a7681` (2 red / 1 green, hit 13/13 in the green,
      MTP healthy) -- variance false-positive, not the migration; re-baseline
      or move to `probe`. STILL PENDING (detached): TTFT/`--cache-reuse` A/B.
- [x] **UI click latency** — PROFILED then FIXED 2026-07-22. The profile
      cleared every suspect in the item: the server is already threaded
      (`ThreadingHTTPServer`), endpoints are 8-54 ms, a 1.8 MB session ships in
      13 ms, and every tab renders in 22-54 ms — an idle studio is not slow.
      The cost is only paid **while a reply streams**: `feed()` wrote
      `log.scrollTop` once per delta, and each write forces a synchronous
      layout of the whole log. Measured through the real studio (Playwright,
      4.3k-node session): **11.5 ms per delta**, so at 60 tok/s roughly 66 % of
      the main thread goes to layout and every click queues behind it.
      Falsified on the way: the per-delta `scrollHeight` READ costs nothing
      (11.18 vs 11.15 ms with it removed) — the write is the whole cost; and
      `ChatStore.list()` re-reads all 44 session files per call but that is
      37 ms, real but not this bug (it grows linearly — revisit past ~300
      sessions).
      Fix: one `requestAnimationFrame` per frame instead of one write per
      delta → **0.018 ms per delta, ~625x**. Verified end-to-end by driving
      the real `streamChat` with a stubbed fetch. Guard test in
      test_factory_web.py (source-level: the frontend is Node-free on purpose,
      so there is no JS runner).
- [x] **Chat scroll inside its own block** — DONE, closed 2026-07-22 after
      MEASURING the half that was still written as open. The containment was
      already shipped on 07-14 (`3dfcc33`) and it holds: driven through the
      real studio on a 790-node agent session, the document does not scroll
      (`scrollHeight == clientHeight`) and `.chat-log` is its own container
      (67 233 px of content in 1 018 px), still true at a 620 px viewport.
      The item was stale, not open.
      The complaint it described is REAL somewhere else, and that is now
      fixed: the **job detail** view. Log and diff panes were unbounded, so
      one job page measured **8416 px on a 620 px viewport** — reading the
      tail of a log scrolled the banner, the verdict and the Apply button off
      screen. Both panes now scroll inside themselves (`max-height: 60vh`):
      **1271 px**. Plus the live tail, which that view never had: it
      re-renders whole every 2 s while a job runs, so the `<pre>` is a new
      element each poll and started at the top of the tail — it now pins to
      the bottom, and an operator who scrolled up keeps their exact position
      across re-renders (verified: 300 px preserved through a full render).
      Guard test in test_factory_web.py.
      The magnet half (earlier the same day, side effect of the latency fix):
      stickiness comes from a `scroll` listener instead of a per-delta
      measurement, and it is seeded from where the operator actually is at
      stream start (it used to reset to "stuck" on every `streamChat` call, so
      an agent turn yanked a reader back to the bottom at every tool
      round-trip). Playwright-verified: follows at the bottom, stays put when
      scrolled up, re-attaches on return.
      Lesson for the rest of this lane: the operator-reported items date from
      07-15 and several may already be fixed — measure before implementing.
**Staleness sweep, 2026-07-22.** The four items below were all written from
the operator's 07-15 pass and all four were already shipped; each was closed
against live evidence, not source reading. Measuring first cost ~20 minutes
and saved four implementations. Anything still open in this lane that predates
07-19 deserves the same treatment before a line of code is written.
- [x] **Live context gauge** — SHIPPED, verified live 2026-07-22 on a real 7b
      turn: the status line redraws at 1 Hz as `⏳ <phase> — <durée> · ctx ~N %`
      (`agentStatus().draw`, app.js), ⚠ saturé past 95 %. The end-of-turn meta
      keeps the exact count (`ctx 39/65536`).
- [x] **num_ctx from the UI** — SHIPPED: a per-session `chat-ctx` select in
      the chat banner (8k/16k/32k/64k/128k), clamped to CHAT_NUM_CTX; it also
      updates the live gauge's denominator. No config edit needed.
- [x] **Model load state visible** — SHIPPED: the turn opens as
      **"chargement modèle + prompt"** and only switches to "réflexion" on the
      first event, so load time is never counted as thinking (measured live:
      4 s of load, then `✓ terminé · 102 tok/s`). Per-model state is in the
      Modèles tab, and the chat banner carries a dot ("○ non chargé — le
      premier message paiera le chargement").
- [x] **Thinking/writing tok/s split in UI** — SHIPPED: `réflexion 1.2 s
      (~167 tok, 134 tok/s) · rédaction 0.3 s (~57 tok, 219 tok/s)`, eval_count
      split by the char share of each phase. Verified on 206 rendered meta
      lines of the 07-19 35b session.
- [x] **Bug: agent turn dies silently on a text tool-call** — SHIPPED
      2026-07-19 (`b6ac6df`): qwen3-coder:30b emits its tool call as XML
      content (`<function=..><parameter=..>`), llama-server `--jinja` doesn't
      parse it into structured tool_calls, so `_agent_turn` saw no `calls` and
      ended the turn as a final answer (operator's "puis plus rien").
      Root-caused via isolated curl repro (`finish:stop`, `tool_calls:False`,
      XML in content). Fix = `llama_client.parse_text_tool_calls` fallback
      (XML + Hermes JSON), model-agnostic; 6 new tests, 603 green.
- [x] **Bug: agent says "je ne peux pas modifier les fichiers"** — STALE,
      closed 2026-07-22 on transcript evidence rather than a live repro.
      Swept all 44 archived sessions for refusal phrasings in assistant
      *content*. Six matches, and only two are this bug: `c_1545c130` ("I
      cannot directly commit and push... I don't have access to git commands")
      and `c_bb6e4b91` ("Since I cannot modify the actual code files due to
      environment limitations"). Both are 2026-07-13, both on `coder:latest` —
      a Modelfile-compiled Ollama model retired with Ollama on 07-19. The
      other four are correct behaviour, not the bug: two refusals to write
      *outside* the workspace (`c_5a2a73ed`), one plain chat session with no
      tools at all (`c_c4315102`, `kind=None`), one "no access to your running
      system" about live profiling.
      Root cause was fixed 2026-07-14 by `14de6bd`, which put "Never claim you
      cannot modify files, run commands or use git -- you can, via those
      tools" in `AGENT_SYSTEM`. It demonstrably works: in the most recent
      agent session (`c_1a722191`, 07-19, 35b) the model's own thinking reads
      "The user is reminding me that I have full write access to the workspace
      and should not claim I can't modify files" — and it proceeds to call the
      tool. Nine agent sessions post-fix, six of which called a write/exec
      tool, zero refusals. Two hypotheses checked and falsified on the way:
      the write tools are in `agent_tools.TOOLS`, and a curated toolset does
      NOT drop them (`load_toolsets` defaults `native` to all).
      Reopen if it recurs on a CURRENT model — that would be a different bug.
- [x] **Tool capability per model** — SHIPPED 2026-07-22 (E2E audit §3). An
      agent session on `qwen2.5-coder:7b` answered ` ```" ` and ended: no tool
      call, no explanation, empty bubble — not the 07-19 plumbing bug, the
      template carries the Hermes `tools` block and an isolated repro outside
      the factory returns the same garbage. `tools = false` now declares the
      exception in factory.toml (default true); the server refuses an agent
      session, or a switch to, a model that cannot call tools, while plain
      chat on it stays allowed. UI: an `outils` column in Modèles, a new agent
      session defaults to the first **capable** model (it defaulted to the
      first of the catalogue — alphabetically the incapable one), and the
      option reads "— sans outils" and is disabled in an agent session.
- [x] **regression_cmd typed on one line is accepted silently** — SHIPPED
      2026-07-22 (E2E audit §4). `add_project` refuses an argument that starts
      with `-` and contains a space: a flag never carries one, a path may, so
      `["C:/some dir/run.py"]` stays legal.
- [x] **`waiting_gpu` says who holds the GPU** — SHIPPED 2026-07-22 (E2E audit
      §5): the job page names the holder ("une session de chat
      (qwen3-coder:30b)") and links to the Modèles tab to release it, instead
      of leaving the operator to wait out the 600 s idle timer.
- [x] **File pickers cap their rows** — SHIPPED 2026-07-22 (E2E audit §6):
      60 per picker instead of 200, ticked files first so a selection can
      never hide behind the cap, and the remainder counted out loud.
- [ ] **PDF reading** — uploads exist but agents can't read PDFs; add an
      extraction tool (text-first, OCR later if a consumer appears).
- [x] **Markdown rendering in the studio** — SHIPPED 2026-07-22 (`6b7e6ac`).
      Scope chosen from measurement, not taste: on the 44 archived sessions,
      50 % of non-empty assistant messages carry Markdown — inline code 41 %,
      bold 23 %, ordered lists 13 %, headings 9 %, bullets 8 %, fences 3 %,
      tables 2 %, quotes 0 %. `harness/web/markdown.js` covers that head; the
      rest survives as literal text (tables and quotes are NOT rendered — they
      stay readable, and 2 % did not earn a table parser).
      Two invariants: (1) the parse never runs per streamed delta — a bubble
      stays raw text until it stops growing (tool call, approval gate, end of
      turn), because per-delta work on this path is exactly what made the
      studio slow; (2) rendering builds elements, never `innerHTML` — this
      text comes from a model and from files on disk. Verified in the real
      page: a `<script>` inside a fence yields zero script nodes.
      `mdParse` is pure (string → block tree, no DOM) so pytest drives it
      through `node` (14 cases: unclosed fences, unclosed markers staying
      literal, code winning over bold inside it). The studio stays Node-free —
      no build step, no dependency; only the test harness uses node.
      Tool outputs stay monospace and faithful (a log or a diff must not be
      reflowed); user bubbles are rendered too. Styling of the rendered blocks
      is deliberately minimal — the DA overhaul below owns the look.
- [x] **Studio favicon** — STALE, closed 2026-07-22: an inline SVG favicon
      (factory silhouette) has been in `index.html` since `e2e263c`, 07-10,
      three days before the item was written. Restyle it with the DA pass.
- [ ] **Dogfooding pass: Claude drives the studio** — Claude uses the tool
      itself (UI via Playwright, or CLI) on real tasks, files every breakage
      and fixes as it goes. Feeds the Studio E2E audit; prerequisite work:
      model routing + personas stable enough to be driven.
- [ ] **Reuse claude-code assets** — can local agents consume the existing
      claude-code files (CLAUDE.md, skills, plugins, MCP configs) instead of
      re-inventing agent.md content? Ties into "agent.md strategy" and
      "Global memory".
- [x] **Auto-compact chat history** — DONE 2026-07-14. Compaction existed
      (spec 07-12) but never fired: the token estimator ignored `tool_calls`
      payloads, so agent prompts overflowed 65536 while the estimate approved
      them. Fixed: wire_chars counts tool_calls, eviction empties old call
      arguments, and a backstop forces compaction when the previous turn's
      REAL Ollama counters hit 90 % of num_ctx. Leftover oddity for
      session-audit: that turn reported `réflexion 0.7 s (~6435 tok)` — the
      thinking wall-clock split looks wrong when the reply is cut.
- [x] **Studio E2E audit** — RUN 2026-07-22, full report
      `audits/20260722-studio-e2e.md`. Every flow in the item was driven on the
      real studio with real models: 6 tabs, chat, agent session, approval,
      **refresh mid-run** (a pending approval survives a full reload — server
      state, nothing client-held), auto mode, unload, add-project, delegate,
      verdict, apply. Two real bugs found, both fixed the same day (§1 ctx
      gauge / compaction backstop, §2 judge-never-ran); four smaller ones filed
      below. Both real bugs were caught by *reading the numbers on screen*,
      with green unit tests all around them.
- [x] **Sessions survive the UI** — SHIPPED 2026-07-22 (`db49f1c`, `5f9464e`),
      spec `docs/design/specs/2026-07-22-server-side-turns-design.md`.
      MEASURED FIRST, and the item's own hypothesis was half wrong: nothing
      leaked or orphaned — the partial was persisted, tool effects kept, GPU
      released. The turn simply DIED, because it was the body of the POST
      (`_stream_events` → `gen.close()` on BrokenPipe). Chat lost its tail
      (4050 chars kept, rest gone); an agent session lost its task mid-way
      *after* its tool effects had landed (read done, `rapport.md` never
      written).
      Fix: `harness/turn_runner.py` runs the generator in its own thread and
      buffers events; connections are viewers with cursors, hanging up closes
      only the reader. `GET /api/chats/<id>/stream?cursor=N` re-attaches,
      `turn_starts_at` tells the page where to stop drawing the stored
      transcript so the replay does not double the tool chips, and `running`
      makes it automatic on load. Stop became `POST /stop`: it used to work
      ONLY as a side effect of dropping the connection, so this change would
      have silently broken it — the trap worth remembering when decoupling
      anything from its transport.
      Verified live on the real studio: cut at 654 chars → re-attach replays
      them and follows to done, 3704 chars persisted, no error flag; the agent
      run finishes its four tool calls after the cut. Regression test cuts the
      socket with SO_LINGER 0 — a plain close lets small writes land in the
      kernel buffer, so the test would have passed for the wrong reason.
- [x] **Drag-n-drop files into chat** — DONE 2026-07-14: agent sessions get
      the file in `<workspace>/uploads/` (upload endpoint, 5 MB cap, bare
      filename only), plain chats get the text inlined in the composer.
- [x] **Timestamps on every log/chat line** — DONE 2026-07-14: ts was already
      stored; now rendered on every bubble and tool chip.
- [ ] **Hybrid controller: Claude plans, local executes** — Fable/Opus writes
      the brief + acceptance criteria, local agents grind, Claude reviews.
      This IS /goal v1 with Claude as the manager; the contract is the brief
      format, not more chat glue. (Confirmed direction 2026-07-14.)
      **Session 1 ran 2026-07-14** (`audits/20260714-hybrid-session-1.md`):
      2/2 real ROADMAP items delegated, reviewed, merged; 1 harness bug found
      and fixed (fence extraction). Next sessions: harder tasks (does the 7b
      ceiling hold?), then a non-factory pytest project, then vitest.
- [x] **Ladder early-abort on structurally identical failures** — DONE
      2026-07-14: `loop_job.py` + `loop.py` abort the run as
      `failure_reason: "structural"` after 3 identical abnormal-exit
      signatures (returncode + normalized output tail), instead of climbing
      the rungs. An honest red run breaks the streak. j_bc119315's 688 s
      would now cost ~3 attempts on the first rung.
- [x] **Degenerate-generation guard (RepetitionGuard)** — DONE 2026-07-14:
      the 07-14 chat session lost minutes of GPU per occurrence to thinking
      loops (134 KB, 1909 lines / 35 unique — see
      `audits/20260714-agent-chat-postmortem.md`). `chat_stream` now watches
      the last 40 streamed lines: ≤ 8 unique ⇒ drop the connection, keep the
      partial, tell the operator (`stopped: "repetition"`); 120 k-char
      thinking cap as backstop. Agent turns stop instead of re-prompting a
      looping model.
- [x] **Plugins/tools for local agents** — DONE 2026-07-15/16: the real
      consumer appeared (chat/agent sessions wanting fetch/search/git/browser).
      `harness/mcp_client.py` (stdlib stdio JSON-RPC, no SDK on Python 3.9) +
      `harness/mcp_registry.py` (namespacing, ~15-tool budget) +
      `[mcp.servers.*]`/`[toolsets.*]` in `factory.toml` (fetch, search, git,
      playwright — trimmed to stay inside budget). Native tools and MCP tools
      mix per session.
- [x] **session-audit job** — analysis core DONE 2026-07-15
      (`harness/session_audit.py`, j_5a1e2ee8): analyze/to_markdown/audit_file
      over a `jobs/chats/*.json` — loops, wasted calls, errors, tok/s, verdict.
      Shipped BY the ladder and it took rung 4 (35b) after 7b/14b/30b all
      plateaued at 9-10/14 (`audits/20260715-hybrid-session-2.md`). Still open:
      wire it as a job type / CLI sweep over all chats. (Supersedes "Agent
      session analytics" below for v1.)
- [ ] **Per-attempt failing-test names in log.txt** — loop_job only records
      counts; whether the lower rungs die on the SAME tests is the routing
      evidence for "start harder tasks at rung 3/4" (hybrid session 2,
      lesson 5).
- [x] **agent.md content pass** — MEASURED 2026-07-23, **FALSIFIED at this
      scale**: `experiments/results/20260723_agent_md_ab.md`, rig
      `experiments/agent_md_variants/`. 7 tasks x 5 runs x 2 variants on
      qwen3-coder:30b through the real `agent_reply`, only
      `GLOBAL_AGENT_MD` changing. Current agent.md and a shorter imperative
      one with good/bad tool-call examples: **35/35 both**, 9 failed tool
      results each, and `edit_file` "0 matches" fired **zero times in 80
      sessions** — the metric the rewrite exists to move never moved because
      it never fired. Likeliest reason: `AGENT_SYSTEM` already carries the
      operative rules (read before edit, "0 matches" ⇒ re-read, prefer
      search/read_file over the shell), so the file has nothing left to add.
      Falsified, not deleted: re-open on LONG multi-step sessions, where those
      failure modes actually occur. Two measurement bugs found first, both of
      which would have produced a confident wrong answer — the completion
      check scored the test's *shape* (2 correct runs marked failed), and
      `tool_errors` ignored non-zero exit codes.
- [x] **`python` on PATH has no pytest** — FOUND then FIXED 2026-07-23, and
      the fix is measured. Found by the agent.md A/B: **every one of the 10
      hard-task sessions**, both arms, burned two round-trips discovering that
      `python` is a 3.12 without pytest, then that its unittest fallback
      reports "NO TESTS RAN" on function-style tests — 20 of the 24 failed
      tool results in that pass, and the largest single source of wasted calls
      measured so far. Fixed at the source (`pip install pytest` on the 3.12,
      now 9.1.1) rather than by prompt wording, which would only have taught
      the agent to route around it — and at a cost, since `AGENT_SYSTEM` is
      the KV prefix. Re-measured on the same task and model: failed tool
      results **2.4 → 0 per session**, shell calls **5.8 → 1.0**, tool calls
      13.0 → 5.0 (A) and 11.6 → 7.0 (B), wall clock 28.7 → 15.9 s (A) and
      21.5 → 11.8 s (B). Checked before trusting it: the suite is **734 green
      under 3.12 as well**, so installing pytest did not turn a loud failure
      ("no module named pytest") into a quiet wrong one (a red regression on a
      second interpreter). `py -3.9` stays the documented standard for the
      hooks and the studio — this only removes the trap for anything that
      reaches for a bare `python`.
- [ ] **128k ctx bench before making it a default** — 64k costs ~7 % vs 32k;
      measure 128k on the same suite before touching defaults. Plus remaining
      micro tok/s levers: see `audits/Claude-Fable.txt`.
- [ ] **DA / design overhaul** — the studio works but looks utilitarian. One
      coherent direction (type scale, spacing, color system, empty states),
      applied everywhere at once. Needs a real design pass with Martin's
      taste in the loop, not incremental CSS tweaks.
- [ ] **Structured missions** — for standalone use: a brainstorm step (dedicated
      persona) → plan → execute pipeline instead of dumping a goal on one agent.
      Ties into /goal v1; the chat agent is the executor, not the planner.
- [ ] **Personas in chat** — pick a persona per agent session (coder, architect,
      reviewer…) and switch mid-session; today the agent runs one hardcoded
      system prompt. Dynamic invocation ("@reviewer look at this diff") later.
- [ ] **Global memory** — persistent operator/project memory across sessions
      (claude-code-style memory files, maybe shared with the delegation lane).
      Design question: where it lives, what writes it, how it loads (budget).
- [ ] **agent.md strategy** — factory rules live in `agent.md` (repo root,
      injected only when the workspace has one). Decide: a factory-global
      rules file injected in EVERY agent session regardless of workspace +
      the workspace's own AGENT.md/CLAUDE.md on top. Also: what to do with
      the root-level .md sprawl (TODO.txt, TODO_chat_improvements.md…).
- [x] **Models info page** — DONE 2026-07-14 (family, params, quant, ctx max,
      capabilities via /api/show). Still open: measured tok/s on this box
      from benchmark history, once the benchmark baseline exists.
- [ ] **Agent session analytics** — mine `jobs/chats/` for failure patterns
      (loops, wasted calls, tool errors) the way the 07-14 post-mortem did by
      hand; feed the findings back into prompts and tool design.
- [ ] **Deep chat/agent benchmarks** — extend `benchmark.py` beyond seeded
      tasks: multi-turn agent scenarios, tok/s split thinking/writing (metrics
      now exist), prefill vs gen, per num_ctx.
- [ ] **Git policy for local agents** — they already reach git through
      run_command (unconfined). 2026-07-14: agent workspaces are now
      auto-`git init`ed at session creation (rule 7 was dead letter in a bare
      directory — weeks of sandbox work sat uncommitted). Still to decide:
      allowed verbs (status/diff/add/commit on the workspace), forbidden
      (push --force, stash — see agent.md §7), and whether commits should be
      automatic per validated step. Depends on run_command confinement below.
- [ ] **Lessons auto-tuning** — deferred by operator decision 2026-07-22 when
      the lessons loop shipped (`harness/lessons.py`, spec
      `docs/design/specs/2026-07-22-lessons-loop-design.md`). Lessons
      currently only reach the agent's system prompt; letting them turn
      runtime knobs (compaction thresholds, patience) is the next rung, and
      it waits until the lessons have proved they say true things. The
      agent's own attempt at it mutated three constants that did not exist.
      **First proof pass run 2026-07-22** (replay of all 44 archived sessions,
      offline — both systems are arithmetic, so no GPU is needed to audit
      them). Two defects found and fixed the same day: the remembered detail
      was the first line, so MEMORY.md said "failed 3x: ============"; and
      learning re-audited the whole transcript every turn while both stores
      add on merge, so counts inflated per turn with nothing new happening.
      Counts are exactly what auto-tuning would read — it stays deferred until
      it has run on live sessions with the fixes in.
- [x] **Cross-turn failure counting** — SHIPPED 2026-07-23, as the item
      prescribed: `from_session` returns raw counts and the floor moved to
      `workspace_memory.promote()`, which applies `FAILURES_MIN` to the total
      (MEMORY.md + this session) instead of to one slice. Memory now re-reads
      the WHOLE transcript every turn — that is what makes a failure-per-turn
      reach two — and `memory_written` on the session (how much of each command
      it has already put in the file) is what stops the double counting the
      watermark stops for lessons. Lessons keep the watermark: re-auditing a
      transcript really does re-count them, so the two stores diverge on
      purpose. Guard tests: the 2-turn case lands as `failed 2x` through the
      real `agent_reply`, and a quiet "merci" turn still adds nothing.
- [ ] **Lessons do not discriminate** — the replay showed only 3 patterns can
      ever fire (`wasted_tool_calls`, `tool_errors`, `generation_loops`), so
      after a handful of sessions the rendered block is saturated and static:
      the same 361 chars forever, whatever the session did. Cheap enough to
      keep, but it is advice, not a lesson. A lesson worth the name would name
      the *thing* that failed (this command, this file, this tool).
- [x] **Agent memory (anti-redo)** — operator ask 07-14, shipped 2026-07-22
      (`harness/workspace_memory.py`). A shell command that failed twice in a
      session is written to `<workspace>/MEMORY.md` and loads into the system
      prompt of the *next* session there (KV-prefix rule: the block is
      snapshotted per session). Only the `factory:auto` block is rewritten —
      the rest of the file is the operator's. Narrow on purpose: `run_command`
      failures only, because a failed `edit_file` is a fact about the agent
      (a lesson), not about the project. Semantic memories ("tried the ONNX
      export, the opset is too old") still need a hand or an LLM pass — see
      "Global memory" below.
- [x] **Carnet — live working memory (phase 1)** — shipped 2026-07-24
      (`harness/carnet.py`). MEMORY.md answers "what did *past* sessions
      learn"; the carnet answers "what have I already tried *in this turn*",
      which is what the 07-23 image-gen session lacked. A projection of the
      transcript (no store), collapsed by normalized target, injected at the
      wire tail so the KV prefix stays byte-stable. `remember` / `recall` /
      `log_wall` are exposed in every toolset; walls and notes persist to
      MEMORY.md with no floor. Failing results carry a reflection nudge, and
      the hard loop-stop is gone (signal, never block). Phase 2 (plan A + C.2
      + E: falsifiable milestones, stagnation detection, autonomy contract)
      is specified in
      `docs/design/specs/2026-07-23-agent-reliability-loop-design.md`
      and not started.
- [x] **Carnet phases 1-3 — live GPU validation** — RUN 2026-07-25, full audit
      `audits/20260725-live-agent-loop.md`. The thing three phases of tests
      never did: an auto agent, on a GPU, on a real BlitzVolley task in a git
      worktree. What held live: `set_plan` unprompted in all four runs, the
      carnet projection, stagnation (peak 42 vs a threshold of 8, armed on 78
      of 106 prefixes), MEMORY.md written with the failing command, and the
      138 KB `server.js` searched instead of read. What did NOT hold — three
      defects, all invisible to 834 offline tests, all failing in the agent's
      favour: (1) `_FILE_COND_RE` was anchored, so the `file:<path> <prose>`
      every model actually writes was classed as prose — no mechanical tick,
      and `verify_claim` waving every claim through, i.e. **A.2 silently off**;
      (2) an *existence* probe cannot verify a MODIFICATION, on either path —
      the harness ticked 3 steps at `ts = 0.0 s` after the plan (and the agent
      believed it: "I have already implemented the core functionality"), and a
      later run ticked **5/5 steps against an empty diff**; (3) re-ticking a
      done step was a silent idempotent success on a BOOKKEEPING tool, so one
      run called `step_done` on step 5 with identical arguments every 8 s for
      ten minutes and would have run to the 1 h cap. All three fixed with
      tests (844 green): first-token parsing that must still look like a path,
      `_changed_since(workspace, plan["ts"])` on both the tick and the claim,
      and a no-op result that names the open steps, returned under `step_done`
      so it counts as an action and stagnation can see the loop. The last one
      is verified live: the agent got "step 1 was already ticked... still open:
      4" and went and did step 4.
      Lesson worth keeping: **a green offline suite proves the code, not the
      contract — every one of these three only exists because a real model
      writes end conditions the way no test author would.**
- [x] **Ladder escalation closes a real task** — 2026-07-25, j_af40da82
      (blitzvolley, `shared/placement.js`, 12-assertion spec): 7b 2/12 then
      5/12, 30b 8/12 twice with the SAME four failures, 35b 12/12 + regression
      green, 261 s. First time escalation has been seen closing a real task
      rather than a seeded one. The 30b plateau is one line — it copied the UMD
      *export* idiom from the context file and never worked out the *import*
      side (`root.BlobRanks.rankForElo` throws under node), while the 35b wrote
      `require('./ranks.js')` with a global fallback. Not a context problem:
      the spec test it is handed already requires the module in CommonJS.
- [ ] **`run_command` on PowerShell instead of cmd.exe** — filed from the live
      run, where it was the dominant cost: 23 shell calls, 8 failures, one
      cause (forward slashes in a path handed to `cmd.exe`), the model
      alternating between `/` and `\` six times without ever learning, and
      `echo "..." > file` destroying its own correct migration by keeping the
      quotes. 21 of the 23 calls were re-reading a file `read_file` covers.
      PowerShell takes both separators, has the unix-ish aliases, runs
      multi-line commands and does not add quotes. NOT a drive-by: it changes
      the shell under every workflow whose evidence was built on cmd.exe, and
      it costs process start-up. Wants a spec and a measurement.
      Explicitly NOT the fix: wording in `SHELL_NOTE_WINDOWS` — prompt wording
      was falsified as a lever on 07-23, and the system prompt is the KV prefix.
- [ ] **Multi-persona debates for big decisions** — operator ask 07-14: on
      design/architecture questions, run 2–3 personas (architect, skeptic,
      simplifier) against each other before implementation instead of one
      agent talking itself into a corner. Cheap: roles are prompts (ADR-001);
      the cost is GPU serialization on one box. Prototype as a chat mode or a
      job type; ties into "Structured missions" and "Personas in chat".
- [ ] **Notifications** — operator ask 07-14: the factory has no way to signal
      "job done / agent blocked / approval needed" when the operator is not
      watching the tab. Options: Windows toast (powershell), ntfy.sh push,
      title-bar badge + favicon first (zero deps).
- [ ] **run_command confinement** — the standing security gap: full user-priv
      shell in auto mode. Options: deny-list, per-workspace allowlist,
      approve-always for shell, or a sandbox user.

## Model selection (hardware: RTX 5070 12 GB VRAM, 31 GB system RAM)
- [ ] **Bench the REAP variants** — `GLM-4.7-Flash-REAP-23B-A3B` (Q4 ≈ 14 GB) and
      `Qwen3.6-28B-REAP20-A3B` (Q4 ≈ 17 GB) vs the resident `qwen3.6:35b` (23 GB).
      Same A3B active params, ~25 % fewer experts, ~-2 % on coding benchmarks — but
      every GB not spilled to RAM is tok/s back. Hypothesis: strictly faster, ~same
      pass rate. Falsify it with `benchmark.py --runs 3`.
- [ ] **Model scout** (`harness/model_scout.py`) — poll the HF API for GGUF repos
      (sort=lastModified, filter on size ≤ 20 GB), diff against `ollama list`,
      emit candidates. Then feed them straight into `benchmark.py`. Discovery is
      cheap; the benchmark suite is what makes the answer trustworthy.
- [ ] **Spill curve** — measure tok/s vs GB-over-12: run one model at Q8/Q6/Q4/Q3
      and plot where the cliff is. Tells us the real VRAM budget, not the folklore.
- [ ] **OpenRouter as the ladder's top rung** — an `openrouter` stage in `ladder`
      for tasks where every local stage plateaus, and as an oracle to label whether
      a task is "hard" or the local model is just weak. Needs a `chat()` backend
      switch in `ollama_client.py` (keep the same signature) + a cost cap.
      Explicitly NOT the default: the factory is local by definition.

## Later
- [ ] **QLoRA a 7B "house coder"** — once run logs give a dataset of good fixes.
      (Fine-tuning 30B is not feasible on 12 GB; 7B is.)
- [x] **Watch the local-LLM optimization talks** — DONE 2026-07-10: five audits
      compiled in `audits/`, synthesized in `docs/audit-synthesis-2026-07.md`.
      Actionable items promoted to Next (llama-server bench, prefill metrics,
      prompt-cache stability).
- [ ] **Tailscale** — remote access to the factory from the laptop; data stays local.
- [ ] **Hardware note** — audit consensus: VRAM > RAM > SSD > CPU; a used
      RTX 3090 24 GB is the best single jump if the factory ever earns its upgrade.
- [ ] **Tool/function calling** — let agents run tests / read repo via Ollama tools
      instead of the harness spoon-feeding context.
- [ ] **Creative lanes** (see docs/vision.md; staff each only when its consumer
      pulls it in):
      - **Image lane** — Flux/SDXL via ComfyUI headless. Consumers: BlitzVolley
        sprites, YouTube thumbnails.
      - **Audio lane** — MusicGen / Stable Audio Open for the sound designer /
        music composer. Consumer: YouTube relax channel (10 h ambient), BlitzVolley
        sounds.
      - **Video editing lane** — ffmpeg assembly (image + soundtrack → upload-ready
        video), the "monteur" agent. Consumer: YouTube channel.
      - **Judge lane** — visual/artistic/audio judges + E2E final-client agent
        (Playwright); judges rank N variants against PO-written criteria, and are
        benchmarked against Martin's own calls before being trusted.
      - 3D lane explicitly deferred (no consumer yet).
- [ ] **Continuous multi-agent project loop** — once the bounded loops AND
      run_command confinement land: agents with different personas answering
      and challenging each other non-stop on one project until it ships
      (extends "Multi-persona debates" + /goal). Martin 18/07: explicitly
      AFTER the reliability lane; also mind the safety of anything running
      unattended.
- [ ] **Upgrade Python** to 3.12/3.13 as the factory standard (current: 3.9 EOL).

## Open questions
- Best generator/verifier model pairing for cost vs quality?
- Does higher temp on Coder ever help escape a stuck loop, or just add noise?
- How much does injecting agent.md actually change behavior? (A/B it.)
- Do project-specific personas (`personas/<project>/pm.md`) beat generic ones, or
  is the project context better injected as task files? (ADR-001 says persona, not
  model — but it says nothing about per-project personas.) Concrete candidate:
  lucena would want a problem composer, a "grandmaster" reviewer, a casual-player
  persona. Caveat: a local 30B has no real chess strength — domain *truth* must
  come from a tool (Stockfish), personas only carry taste/UX/pedagogy. Cheap to
  A/B since roles are prompts (ADR-001).
- The harness is stateless: one system + one user message per attempt, no history,
  so nothing is ever compacted. Is `last_output[-3000:]` the right failure summary,
  or should a cheap model summarize the pytest output instead?
