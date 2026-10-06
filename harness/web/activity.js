// What the operator sees while the agent works: one live line, rewritten in
// place, instead of every thought and every tool blob stacked in the log.
//
// Pure on purpose -- no DOM, no globals. app.js renders what this returns,
// and pytest drives it through node (test_web_activity.py), the same deal
// as markdown.js.

const CLIP = 80;

function clip(s) {
  s = s == null ? "" : String(s);
  return s.length > CLIP ? s.slice(0, CLIP - 1) + "…" : s;
}

// name -> icon id (see icons.svg), verb, and how to read the target out of
// the call arguments. Argument names come from the tool definitions
// themselves: agent_tools.py, carnet.py, factory_mcp.py.
const TOOLS = {
  list_dir:         { icon: "file",     verb: "liste",    t: (a) => a.path },
  read_file:        { icon: "file",     verb: "lit",      t: (a) => a.path },
  search:           { icon: "search",   verb: "cherche",  t: (a) => a.pattern },
  write_file:       { icon: "pencil",   verb: "ecrit",    t: (a) => a.path },
  edit_file:        { icon: "pencil",   verb: "modifie",  t: (a) => a.path },
  run_command:      { icon: "terminal", verb: "",         t: (a) => a.command },
  remember:         { icon: "memory",   verb: "note",     t: (a) => a.note },
  recall:           { icon: "memory",   verb: "relit",    t: (a) => a.query },
  log_wall:         { icon: "wall",     verb: "mur",      t: (a) => a.wall },
  set_plan:         { icon: "plan",     verb: "pose le plan",
                      t: (a) => (a.steps || []).length + " etapes" },
  step_done:        { icon: "plan",     verb: "etape",    t: (a) => a.step },
  ask_operator:     { icon: "hand",     verb: "demande a l'operateur",
                      t: (a) => a.question },
  request_resource: { icon: "hand",     verb: "demande une ressource",
                      t: (a) => a.resource },
  delegate:         { icon: "gear",     verb: "delegue",  t: (a) => a.goal },
  job_status:       { icon: "gear",     verb: "suit le job",   t: (a) => a.job_id },
  job_result:       { icon: "gear",     verb: "lit le job",    t: (a) => a.job_id },
  job_log:          { icon: "gear",     verb: "lit le log",    t: (a) => a.job_id },
  job_cancel:       { icon: "gear",     verb: "annule le job", t: (a) => a.job_id },
};

// An unknown tool is not an error -- it is a toolset that moved. Show its
// name and the first thing that looks like a value.
function firstValue(args) {
  for (const v of Object.values(args)) {
    if (typeof v === "string" || typeof v === "number") return v;
  }
  return "";
}

function describeCall(name, args) {
  const a = args || {};
  const e = TOOLS[name];
  if (!e) return { icon: "tool", verb: String(name), target: clip(firstValue(a)) };
  let target = "";
  try { target = e.t(a); } catch (_) { target = ""; }
  return { icon: e.icon, verb: e.verb, target: clip(target) };
}

// A turn runs from one user message to the next. Anything sitting in front
// of the first user message -- a resumed session, a system preamble -- goes
// into a lead-less turn rather than being dropped on the floor.
function groupTurns(messages) {
  const turns = [];
  let cur = null;
  for (const m of messages || []) {
    if (m.role === "user") {
      cur = { lead: m, items: [] };
      turns.push(cur);
      continue;
    }
    if (!cur) { cur = { lead: null, items: [] }; turns.push(cur); }
    cur.items.push(m);
  }
  return turns;
}

const READS = new Set(["read_file", "list_dir", "search"]);
const WRITES = new Set(["write_file", "edit_file"]);
const JOBS = new Set(["delegate", "job_status", "job_result", "job_log", "job_cancel"]);
const PLANS = new Set(["set_plan", "step_done"]);
const NOTES = new Set(["remember", "recall", "log_wall", "ask_operator", "request_resource"]);

function plural(n, one, many) { return n + " " + (n > 1 ? many : one); }

function formatRecap(turn) {
  const tools = (turn.items || []).filter((m) => m.role === "tool");
  if (!tools.length) return "";
  let reads = 0, writes = 0, commands = 0, jobs = 0, plans = 0, notes = 0, autres = 0;
  for (const m of tools) {
    if (READS.has(m.tool_name)) reads++;
    else if (WRITES.has(m.tool_name)) writes++;
    else if (m.tool_name === "run_command") commands++;
    else if (JOBS.has(m.tool_name)) jobs++;
    else if (PLANS.has(m.tool_name)) plans++;
    else if (NOTES.has(m.tool_name)) notes++;
    else autres++;
  }
  const parts = [plural(tools.length, "outil", "outils")];
  const start = turn.lead ? turn.lead.ts : (turn.items[0] || {}).ts;
  const end = (turn.items[turn.items.length - 1] || {}).ts;
  if (start != null && end != null) parts.push(Math.max(0, Math.round(end - start)) + " s");
  const what = [];
  if (reads) what.push(plural(reads, "lu", "lus"));
  if (writes) what.push(plural(writes, "ecrit", "ecrits"));
  if (commands) what.push(plural(commands, "commande", "commandes"));
  if (jobs) what.push(plural(jobs, "job", "jobs"));
  if (plans) what.push(plural(plans, "etape de plan", "etapes de plan"));
  if (notes) what.push(plural(notes, "note", "notes"));
  if (autres) what.push(plural(autres, "autre", "autres"));
  if (what.length) parts.push(what.join(", "));
  return parts.join(" · ");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { describeCall, groupTurns, formatRecap, TOOLS };  // for the pytest harness only
}
