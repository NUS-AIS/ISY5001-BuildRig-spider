/**
 * BuildRig Pi Runtime worker.
 *
 * A Pi Agent Core supervisor plans a configuration by itself: it decides which tool to call next,
 * reads the result, fixes failed checks and submits when the server accepts the build. All data,
 * rules and retrieval live in the Python backend and are reached through token-protected internal
 * endpoints, so the runtime cannot bypass the budget or compatibility checks:
 *
 *   draft_build    -> POST /api/v1/internal/options/draft       deterministic first draft to review and repair
 *   find_parts     -> POST /api/v1/internal/candidates          compatible in-stock offers
 *   select_part    -> keeps the build in worker state; only offers returned by find_parts are accepted
 *   check_build    -> POST /api/v1/internal/options/assemble    server-side assembly + validation
 *   find_evidence  -> POST /api/v1/internal/retrieval/hybrid    BM25 + dense + graph retrieval
 *   submit_build   -> server-side validation gate; a failed check rejects the submission
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { Agent, type AgentTool } from "@earendil-works/pi-agent-core";
import { createModels, createProvider, Type, type Model } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

type Json = Record<string, any>;

const port = Number(process.env.PI_PORT || 8090);
const apiBase = (process.env.BUILDRIG_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const internalToken = process.env.BUILDRIG_INTERNAL_API_TOKEN || "";
const ollamaBase = (process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434").replace(/\/$/, "");
const modelId = process.env.BUILDRIG_MODEL || "qwen3:8b";
const maxToolCalls = Number(process.env.PI_MAX_TOOL_CALLS || 24);
// The window Ollama actually serves: its OpenAI-compatible API takes no per-request context size, so the
// server default (OLLAMA_CONTEXT_LENGTH) must match the value the Python backend requests, or every switch
// between the two reloads the model.
const contextLength = Number(process.env.BUILDRIG_OLLAMA_NUM_CTX || 8192);
// The API waits BUILDRIG_PI_RUNTIME_TIMEOUT_SECONDS for the answer and discards a run that takes longer.
// Stopping a little earlier returns what was tried, with an explanation, instead of a bare timeout.
const maxSeconds = Number(process.env.PI_MAX_SECONDS || Math.max(Number(process.env.BUILDRIG_PI_RUNTIME_TIMEOUT_SECONDS || 300) - 30, 30));
const DESKTOP = ["cpu", "motherboard", "ram", "gpu", "psu", "case", "ssd", "cooler"];

async function readJson(request: IncomingMessage): Promise<Json> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function reply(response: ServerResponse, status: number, body: Json) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

async function backend(path: string, body: Json): Promise<Json> {
  const response = await fetch(`${apiBase}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-internal-token": internalToken },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`Backend ${path} failed (${response.status}): ${await response.text()}`);
  return response.json() as Promise<Json>;
}

/** Tell the API what the supervisor is doing, so the page shows progress during a long run. Never fatal. */
async function report(runId: string, stage: string, message: string) {
  try {
    await fetch(`${apiBase}/api/v1/internal/runs/${runId}/progress`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-internal-token": internalToken },
      body: JSON.stringify({ stage, message }),
    });
  } catch { /* progress is best effort */ }
}

const STAGES: Record<string, (args: Json) => [string, string]> = {
  draft_build: () => ["pi_drafting_a_build", "Pi is drafting a starting build"],
  find_parts: (a) => [`pi_searching_${String(a.category || "parts").replace(/[^a-z0-9]+/gi, "_").toLowerCase()}`, `Pi is searching ${a.category || "parts"}`],
  select_part: (a) => ["pi_changing_a_part", `Pi is changing the ${a.category || "part"}`],
  check_build: () => ["pi_validating_the_build", "Pi is validating the build"],
  find_evidence: () => ["pi_retrieving_evidence", "Pi is retrieving evidence"],
  submit_build: () => ["pi_submitting_the_build", "Pi is submitting the build"],
};

function text(value: unknown) {
  return [{ type: "text" as const, text: typeof value === "string" ? value : JSON.stringify(value) }];
}

function createModel() {
  const model: Model<"openai-completions"> = {
    id: modelId,
    name: `${modelId} (Ollama)`,
    api: "openai-completions",
    provider: "ollama",
    baseUrl: `${ollamaBase}/v1`,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: contextLength,
    maxTokens: 1200,
    compat: { supportsDeveloperRole: false, supportsReasoningEffort: false, supportsStore: false },
  };
  const provider = createProvider({
    id: "ollama",
    name: "Ollama",
    baseUrl: `${ollamaBase}/v1`,
    auth: { apiKey: { name: "Ollama", resolve: async () => ({ auth: {} }) } },
    models: [model],
    api: openAICompletionsApi(),
  });
  const models = createModels();
  models.setProvider(provider);
  return { model, models };
}

/** State shared by the tools of one supervisor session. The build lives here, not in the model's memory. */
interface Session {
  runId: string;
  deviceType: "desktop" | "laptop";
  requirements: Json;
  build: Record<string, Json>;           // category -> selected offer (from find_parts results only)
  seen: Record<string, Json>;            // offer_id -> offer returned by find_parts in this session
  submitted?: { title: string; reasons: string[]; assembled: Json };
  emptySearches: Record<string, number>; // category -> find_parts calls in a row that returned nothing
  deadline: number;                      // epoch ms after which the run stops and reports what it tried
  timedOut: boolean;
  drafted: boolean;                      // draft_build has loaded the starting build
  budgetAdvice: boolean;                 // the last failed check came with server-computed swaps
  shortfallSgd?: number;                 // set when no cheaper compatible part can close the budget gap
  attempts: Json[];
  toolCalls: Json[];
  usage: { calls: number; input_tokens: number; output_tokens: number };
}

const SHORT_SPECS = ["socket", "memory_type", "form_factor", "max_form_factor", "capacity_gb", "wattage_w", "tdp_w",
  "chip", "vram_gb", "length_mm", "max_gpu_length_mm", "integrated_graphics", "ram_gb", "storage_gb"];

function compact(offer: Json): Json {
  const specs: Json = {};
  for (const key of SHORT_SPECS) if (offer.specs?.[key] != null) specs[key] = offer.specs[key];
  return { offer_id: offer.offer_id, name: String(offer.name).slice(0, 70), price_sgd: offer.price_sgd, specs };
}

function required(session: Session): string[] {
  if (session.deviceType === "laptop") return ["laptop"];
  const owned = new Set((session.requirements.owned_components || []).map((o: Json) => o.category));
  const office = (session.requirements.workloads || []).every((w: string) => !/gam|solidworks|ansys|blender|video|render|learning|ai\b/i.test(w));
  const hard = session.requirements.hard_constraints || {};
  const cardRequired = Boolean(hard.minimum_gpu_memory_gb || hard.gpu_vendor || hard.minimum_gpu_model);
  const noCard = Boolean(hard.no_graphics_card) && !session.build.gpu?.locked;
  const igpu = session.build.cpu?.specs?.integrated_graphics === true && office && !session.build.gpu && !cardRequired;
  return DESKTOP.filter((c) => !owned.has(c) && !((igpu || noCard) && c === "gpu"));
}

/** Where the build stands and which compatibility filters the next part needs. */
function progress(session: Session): Json {
  const b = session.build;
  const spent = Object.values(b).reduce((sum, o) => sum + o.price_sgd, 0);
  const budget = session.requirements.budget.maximum_minor / 100;
  const missing = required(session).filter((c) => !b[c]);
  const next = missing[0];
  const hint: Json = {};
  if (next === "motherboard" && b.cpu?.specs?.socket) hint.require = { socket: b.cpu.specs.socket };
  if (next === "cpu" && b.motherboard?.specs?.socket) hint.require = { socket: b.motherboard.specs.socket };
  if (next === "ram") {
    if (b.motherboard?.specs?.memory_type) hint.require = { memory_type: b.motherboard.specs.memory_type };
    hint.minimum = { capacity_gb: session.requirements.hard_constraints?.minimum_memory_gb || 16 };
  }
  if (next === "gpu" && session.requirements.hard_constraints?.minimum_gpu_memory_gb) {
    hint.minimum = { vram_gb: session.requirements.hard_constraints.minimum_gpu_memory_gb };
  }
  if (next === "psu" && b.cpu?.specs?.tdp_w != null) {
    hint.minimum = { wattage_w: Math.ceil(((b.cpu.specs.tdp_w || 0) + (b.gpu?.specs?.tdp_w || 0) + 100) * 1.2) };
  }
  if (next === "case" && b.gpu?.specs?.length_mm) hint.minimum = { max_gpu_length_mm: b.gpu.specs.length_mm };
  if (next === "ssd") hint.minimum = { capacity_gb: session.requirements.hard_constraints?.minimum_storage_gb || 500 };
  if (next) hint.max_price_sgd = Math.max(Math.floor(budget - spent - 40 * (missing.length - 1)), 0);
  return {
    current_build: Object.fromEntries(Object.entries(b).map(([c, o]) => [c, `${String(o.name).slice(0, 50)} (S$${o.price_sgd})`])),
    spent_sgd: spent, budget_sgd: budget, still_missing: missing,
    next_step: next ? { call: "find_parts", category: next, ...hint } : { call: "check_build" },
  };
}

/**
 * What a failed validation asks the model to do next. The budget check names only the most expensive
 * part, which may have no cheaper compatible alternative, so an overspend comes with swaps the server
 * has already verified. They are registered as seen offers and can go straight to select_part.
 */
function repairAdvice(session: Session, assembled: Json): Json {
  const v = assembled.validation;
  const repair = assembled.budget_repair;
  const failed = v.checks.filter((c: Json) => c.status === "failed").map((c: Json) =>
    c.code === "budget_limit" && repair ? { code: c.code, reason: c.reason } : { code: c.code, reason: c.reason, replace: c.affected_categories });
  const others = failed.some((c: Json) => c.code !== "budget_limit");
  session.budgetAdvice = false;
  if (!repair) {
    return { failed, then: "Replace the named categories with find_parts + select_part, then check_build again." };
  }
  const swap = (entry: Json) => {
    const offer: Json = { ...compact(entry.alternatives[0]), category: entry.category };
    session.seen[offer.offer_id] = offer;
    return { category: entry.category, offer_id: offer.offer_id, name: offer.name, price_sgd: offer.price_sgd, saves_sgd: entry.saving_minor / 100 };
  };
  const advice: Json = { failed, over_budget_by_sgd: repair.over_minor / 100 };
  if (repair.single_swaps.length) {
    session.budgetAdvice = true;
    advice.any_one_of_these_swaps_fixes_the_budget = repair.single_swaps.slice(0, 3).map(swap);
    advice.then = "Call select_part with ONE of these offers, then check_build. Do not search for other parts.";
  } else if (repair.reachable) {
    session.budgetAdvice = true;
    advice.cheaper_swaps_largest_saving_first = repair.partial_savings.slice(0, 3).map(swap);
    advice.then = "No single swap is enough. Call select_part with the first offer, then check_build for updated advice.";
  } else if (!others) {
    const saved = repair.partial_savings.reduce((sum: number, e: Json) => sum + e.saving_minor, 0);
    session.shortfallSgd = (repair.over_minor - saved) / 100;
    advice.then = "No cheaper compatible parts can close this gap. Stop: the budget cannot be met.";
  }
  if (others) advice.then = `${advice.then || ""} Also replace the categories named by the other failed checks with find_parts + select_part.`.trim();
  return advice;
}

function tools(session: Session): AgentTool<any>[] {
  const log = (tool: string, args: Json, outcome: string) => session.toolCalls.push({ tool, args, outcome });
  const guard = () => {
    if (session.toolCalls.length >= maxToolCalls) throw new Error("Tool call budget exhausted for this run.");
    if (Date.now() >= session.deadline) throw new Error("Time budget exhausted for this run.");
  };
  // Building part by part from an empty build spends the budget on the first category; the draft is
  // a compatible build within budget shares, so every other tool waits for it.
  const needsDraft = (tool: string, args: Json) => {
    if (session.drafted) return null;
    log(tool, args, "needs draft");
    return { content: text("Call draft_build first: it loads a compatible starting build to review and repair."), details: null };
  };
  const assemble = () => backend("/api/v1/internal/options/assemble", {
    run_id: session.runId, device_type: session.deviceType,
    offer_ids: Object.values(session.build).map((o) => o.offer_id), requirements: session.requirements,
  });

  const draftBuild: AgentTool<any> = {
    name: "draft_build",
    label: "Draft a starting build",
    description: "Load a first draft built by the deterministic planner (compatible chain within budget shares). Review and improve it.",
    parameters: Type.Object({}),
    async execute() {
      guard();
      const draft = await backend("/api/v1/internal/options/draft", {
        run_id: session.runId, device_type: session.deviceType, requirements: session.requirements,
      });
      for (const item of draft.items) {
        if (item.owned_by_user || session.build[item.category]?.locked) continue;
        const offer: Json = { ...compact(item), category: item.category, locked: item.locked };
        session.build[item.category] = offer;
        session.seen[offer.offer_id] = offer;
      }
      session.drafted = true;
      log("draft_build", {}, `${draft.items.length} parts`);
      return { content: text({ ...progress(session), then: "Call check_build next." }), details: draft };
    },
  };

  const findParts: AgentTool<any> = {
    name: "find_parts",
    label: "Find compatible parts",
    description:
      "List in-stock offers of one category from the Singapore catalogue, highest price first within the range. " +
      "Use 'require' for exact spec matches ({\"socket\": \"AM5\"}) and 'minimum' for lower bounds ({\"wattage_w\": 750}). " +
      "Each category has its own spec names; an unknown one is rejected with the valid list.",
    parameters: Type.Object({
      category: Type.String({ description: "cpu, motherboard, ram, gpu, psu, case, ssd, cooler or laptop" }),
      max_price_sgd: Type.Optional(Type.Number()),
      min_price_sgd: Type.Optional(Type.Number()),
      require: Type.Optional(Type.Record(Type.String(), Type.Union([Type.String(), Type.Boolean(), Type.Number()]))),
      minimum: Type.Optional(Type.Record(Type.String(), Type.Number())),
    }),
    async execute(_id, raw) {
      const params = raw as Json;
      guard();
      const early = needsDraft("find_parts", params);
      if (early) return early;
      const result = await backend("/api/v1/internal/candidates", {
        run_id: session.runId, category: params.category,
        maximum_minor: params.max_price_sgd != null ? Math.round(params.max_price_sgd * 100) : null,
        minimum_minor: params.min_price_sgd != null ? Math.round(params.min_price_sgd * 100) : null,
        require: params.require || {}, minimum_specs: params.minimum || {}, limit: 5,
      });
      if (result.invalid_filters) {
        log("find_parts", params, "invalid filters");
        const bad = result.invalid_filters;
        return {
          content: text(bad.unknown_category
            ? { error: `Unknown category '${bad.unknown_category}'.`, valid_categories: DESKTOP.concat("laptop") }
            : { error: `A ${params.category} has no spec named ${bad.unknown_keys.join(", ")}.`, valid_spec_names: bad.valid_keys,
                then: "Search again with valid spec names only, or with none." }),
          details: result,
        };
      }
      const offers = result.items.map(compact);
      for (const o of offers) session.seen[o.offer_id] = { ...o, category: params.category };
      log("find_parts", params, `${offers.length} offers`);
      if (offers.length) {
        session.emptySearches[params.category] = 0;
        return { content: text({ offers, then: "Pick one with select_part." }), details: result };
      }
      const empty = (session.emptySearches[params.category] = (session.emptySearches[params.category] || 0) + 1);
      let message = result.cheapest_match_sgd != null
        ? `No in-stock ${params.category} in that price range. The cheapest one matching these filters costs S$${result.cheapest_match_sgd}.`
        : result.cheapest_match_sgd === null
          ? `No in-stock ${params.category} matches these spec filters at any price.`
          : "No matching in-stock offers. Drop a soft preference, never a hard constraint.";
      if (empty >= 2) {
        message += ` Stop searching ${params.category}: ` + (session.budgetAdvice
          ? "use one of the swaps check_build listed." : "keep the current one and change a different part instead.");
      }
      return { content: text(message), details: result };
    },
  };

  const selectPart: AgentTool<any> = {
    name: "select_part",
    label: "Select a part",
    description: "Put one offer returned by find_parts into the build (replaces any part of that category).",
    parameters: Type.Object({ category: Type.String(), offer_id: Type.String() }),
    async execute(_id, raw) {
      const params = raw as Json;
      guard();
      const early = needsDraft("select_part", params);
      if (early) return early;
      const offer = session.seen[params.offer_id];
      if (!offer || offer.category !== params.category) {
        log("select_part", params, "rejected");
        return { content: text("Unknown offer_id for this category. Use an offer_id exactly as returned by find_parts."), details: null };
      }
      if (session.build[params.category]?.locked) {
        log("select_part", params, "locked");
        return { content: text(`The ${params.category} is locked by the user and cannot be changed.`), details: null };
      }
      session.build[params.category] = offer;
      log("select_part", params, "ok");
      return { content: text(progress(session)), details: null };
    },
  };

  const checkBuild: AgentTool<any> = {
    name: "check_build",
    label: "Validate the build",
    description: "Run every deterministic check (budget, stock, socket, memory, PSU, clearance, form factor) on the current build.",
    parameters: Type.Object({}),
    async execute() {
      guard();
      const early = needsDraft("check_build", {});
      if (early) return early;
      const assembled = await assemble();
      const v = assembled.validation;
      session.attempts.push({ offer_ids: Object.values(session.build).map((o) => o.offer_id), status: v.overall_status,
        failed: v.failed_codes, total_minor: v.total_minor });
      log("check_build", {}, v.overall_status);
      const summary = { overall_status: v.overall_status, total_sgd: v.total_minor / 100 };
      if (v.overall_status !== "failed") {
        session.budgetAdvice = false;
        return { content: text({ ...summary, failed: [], then: "Call submit_build." }), details: assembled };
      }
      const advice = repairAdvice(session, assembled);
      // Nothing cheaper exists: end the turn instead of letting the model search for parts that are not there.
      return { content: text({ ...summary, ...advice }), details: assembled, terminate: session.shortfallSgd != null };
    },
  };

  const findEvidence: AgentTool<any> = {
    name: "find_evidence",
    label: "Find evidence",
    description: "Search reviews, listings and graph facts about the parts in the current build.",
    parameters: Type.Object({ query: Type.String() }),
    async execute(_id, raw) {
      const params = raw as Json;
      guard();
      const assembled = await assemble();
      const productIds = assembled.option.items.filter((i: Json) => !i.owned_by_user).map((i: Json) => i.product_id);
      const result = await backend("/api/v1/internal/retrieval/hybrid", {
        run_id: session.runId, query: params.query, snapshot_id: "pinned", product_ids: productIds, top_k: 5,
      });
      log("find_evidence", params, `${result.items.length} items`);
      return { content: text(result.items.map((e: Json) => ({ kind: e.kind, match_level: e.match_level, excerpt: String(e.excerpt).slice(0, 200) }))), details: result };
    },
  };

  const submitBuild: AgentTool<any> = {
    name: "submit_build",
    label: "Submit the build",
    description: "Submit the current build with a short title and 2-4 reasons. The server re-validates and rejects any failed check.",
    parameters: Type.Object({ title: Type.String(), reasons: Type.Array(Type.String()) }),
    async execute(_id, raw) {
      const params = raw as Json;
      guard();
      const missing = required(session).filter((c) => !session.build[c]);
      if (missing.length) {
        log("submit_build", params, "incomplete");
        return { content: text({ accepted: false, still_missing: missing, next_step: progress(session).next_step }), details: null };
      }
      const assembled = await assemble();
      const v = assembled.validation;
      if (v.overall_status === "failed") {
        log("submit_build", params, "rejected");
        return { content: text({ accepted: false, ...repairAdvice(session, assembled) }), details: v, terminate: session.shortfallSgd != null };
      }
      session.submitted = { title: params.title, reasons: params.reasons, assembled };
      log("submit_build", params, "accepted");
      return { content: text({ accepted: true, overall_status: v.overall_status }), details: v, terminate: true };
    },
  };

  return [draftBuild, findParts, selectPart, checkBuild, findEvidence, submitBuild];
}

function briefing(session: Session): string {
  const r = session.requirements;
  const common = `Budget: at most S$${r.budget?.maximum_minor / 100}. Workloads: ${JSON.stringify(r.workloads || [])}. ` +
    `Preferences: ${JSON.stringify(r.preferences || [])}. Hard constraints: ${JSON.stringify(r.hard_constraints || {})}. ` +
    `Locked products (must be included): ${JSON.stringify((r.locked_items || []).map((i: Json) => i.name || i.mention))}. ` +
    `Already owned (do not buy): ${JSON.stringify((r.owned_components || []).map((o: Json) => o.mention))}.`;
  const steps = "Steps: 1) draft_build. 2) check_build. 3) If a check failed, follow its 'then' instruction: when it lists " +
    "ready-made swaps, select_part one of them directly; otherwise replace only the categories it names (find_parts with " +
    "the suggested filters, then select_part). Then check_build again. 4) If a part clearly does not fit the " +
    "workloads or preferences, you may replace it the same way. 5) Optionally find_evidence. 6) submit_build with a title " +
    "and 2-4 reasons taken from tool results. Always act through tool calls.";
  if (session.deviceType === "laptop") return `/no_think Recommend ONE in-stock laptop. ${common} ${steps}`;
  return `/no_think Recommend ONE complete desktop. ${common} ${steps}`;
}

async function superviseOne(runId: string, deviceType: "desktop" | "laptop", requirements: Json, deadline: number) {
  const session: Session = { runId, deviceType, requirements, build: {}, seen: {}, attempts: [], toolCalls: [],
    emptySearches: {}, deadline, timedOut: false, drafted: false, budgetAdvice: false, usage: { calls: 0, input_tokens: 0, output_tokens: 0 } };
  // Locked products are part of the build from the start; the model cannot remove them.
  const lockedIds = (requirements.locked_product_ids || []).map(String);
  if (lockedIds.length) {
    const locked = await backend("/api/v1/internal/candidates", { run_id: runId, category: "any", product_ids: lockedIds });
    for (const item of locked.items) {
      const offer: Json = { ...compact(item), category: item.category };
      const category = (requirements.locked_items || []).find((l: Json) => String(l.product_id) === String(item.product_id))?.category;
      if (category && (deviceType === "laptop") === (category === "laptop") && !session.build[category]) {
        session.build[category] = { ...offer, category, locked: true };
        session.seen[offer.offer_id] = session.build[category];
      }
    }
  }
  const { model, models } = createModel();
  const agent = new Agent({
    initialState: {
      systemPrompt:
        "/no_think You are the BuildRig Pi supervisor for computer purchases in Singapore. Always act through tool calls; never invent " +
        "products, prices or specifications. Hard constraints (budget, locked parts, compatibility) can never be relaxed. " +
        "Answer in English.",
      model,
      thinkingLevel: "off",
      tools: tools(session),
      messages: [],
    },
    streamFn: models.streamSimple.bind(models),
    getApiKey: async () => "ollama",
    sessionId: `${runId}:${deviceType}`,
    toolExecution: "sequential",
  });
  await report(runId, `pi_planning_${deviceType}`, `Pi is planning the ${deviceType}`);
  // A model call can outlast the deadline on its own, so the stop does not wait for the next tool call.
  const stopAtDeadline = setTimeout(() => { session.timedOut = true; agent.abort(); }, Math.max(deadline - Date.now(), 0));
  agent.subscribe(async (event: Json) => {
    if (event.type === "tool_execution_start" && STAGES[event.toolName]) {
      await report(runId, ...STAGES[event.toolName](event.args || {}));
    }
    if (event.type === "tool_execution_end" && session.toolCalls.length >= maxToolCalls) agent.abort();
    if (event.type === "message_end" && event.message?.role === "assistant") {
      session.usage.calls += 1;
      session.usage.input_tokens += Number(event.message.usage?.input || 0);
      session.usage.output_tokens += Number(event.message.usage?.output || 0);
    }
    if (process.env.PI_DEBUG && event.type === "message_end" && event.message?.role === "assistant") {
      const parts = (event.message.content || []).map((p: Json) => p.type === "text" ? `TEXT: ${p.text}` : p.type === "toolCall" ? `CALL: ${p.name} ${JSON.stringify(p.arguments)}` : p.type);
      console.log(`[${deviceType}] ${parts.join(" | ").slice(0, 400)} usage=${JSON.stringify(event.message.usage?.input ?? "")}`);
    }
  });
  try {
    await agent.prompt(briefing(session));
    for (let nudge = 0; nudge < 2 && !session.submitted && session.shortfallSgd == null && !session.timedOut
        && !agent.state.errorMessage && session.toolCalls.length < maxToolCalls; nudge++) {
      await agent.prompt(`/no_think You have not submitted an accepted build yet. Status: ${JSON.stringify(progress(session))}. Continue with tool calls.`);
    }
  } finally {
    clearTimeout(stopAtDeadline);
  }
  // Stopping at the deadline is reported as such, not as whatever error the interruption left behind.
  return { session, error: session.timedOut ? undefined : agent.state.errorMessage };
}

async function execute(payload: Json): Promise<Json> {
  const { run, requirements } = payload;
  const branches: ("desktop" | "laptop")[] = requirements.device_type === "compare" ? ["desktop", "laptop"] : [requirements.device_type];
  const options: Json[] = [];
  const trace: Json[] = [];
  const usage = { calls: 0, input_tokens: 0, output_tokens: 0 };
  const limitations: string[] = [];
  const failures: ("error" | "budget" | "steps")[] = [];
  const deadline = Date.now() + maxSeconds * 1000;
  for (const branch of branches) {
    const { session, error } = await superviseOne(run.id, branch, requirements, deadline);
    trace.push({ agent: `pi_supervisor:${branch}`, tool_calls: session.toolCalls, attempts: session.attempts });
    usage.calls += session.usage.calls; usage.input_tokens += session.usage.input_tokens; usage.output_tokens += session.usage.output_tokens;
    if (!session.submitted) {
      // Say why this branch ended without a build: the three causes call for different actions by the user.
      const last = session.attempts.at(-1);
      const closest = last ? ` Its last attempt cost S$${(last.total_minor / 100).toFixed(0)} and failed: ${last.failed.join(", ")}.` : "";
      if (error) {
        failures.push("error");
        limitations.push(`Pi ${branch} supervisor stopped with an error: ${error}`);
      } else if (session.shortfallSgd != null) {
        failures.push("budget");
        limitations.push(`Pi ${branch} supervisor found no compatible ${branch} within the budget: with the cheapest compatible ` +
          `alternatives its build is still about S$${session.shortfallSgd.toFixed(0)} over.`);
      } else {
        failures.push("steps");
        limitations.push(session.timedOut || Date.now() >= deadline
          ? `Pi ${branch} supervisor reached its ${maxSeconds}-second limit after ${session.toolCalls.length} tool calls without an accepted build.${closest}`
          : `Pi ${branch} supervisor stopped after ${session.toolCalls.length} of ${maxToolCalls} tool calls without an accepted build.${closest}`);
      }
      continue;
    }
    if (error) limitations.push(`Pi ${branch} supervisor stopped with an error: ${error}`);
    const { option } = session.submitted.assembled;
    const offerIds = option.items.filter((i: Json) => i.offer_id).map((i: Json) => i.offer_id);
    const evidence = await backend("/api/v1/internal/retrieval/hybrid", {
      run_id: run.id, query: [...(requirements.workloads || []), ...(requirements.preferences || [])].join(" ") || "reliability",
      snapshot_id: run.snapshot_id, product_ids: option.items.filter((i: Json) => !i.owned_by_user).map((i: Json) => i.product_id), top_k: 8,
    });
    options.push({
      ...option,
      option_id: `pi_${branch}_${offerIds[0]}`,
      title: session.submitted.title,
      reasons: session.submitted.reasons,
      trade_offs: ["Unknown checks are listed with the option and should be confirmed before purchase."],
      validation: session.submitted.assembled.validation,
      evidence: evidence.items,
      retrieval: { backend: evidence.retrieval_backend, degraded_routes: evidence.degraded_routes || [] },
      review: { decision: session.submitted.assembled.validation.overall_status === "unknown" ? "accept_with_unknowns" : "accept", notes: [], source: "server_gate" },
      revision_history: session.attempts.filter((a) => a.status === "failed").map((a, i) => ({ round: i + 1, failed_checks: a.failed, offer_ids: a.offer_ids })),
    });
  }
  const toolCalls = trace.flatMap((t) => t.tool_calls.map((c: Json) => ({ agent_role: t.agent, tool: c.tool, status: c.outcome })));
  // Stopping short is a limit of this run, not evidence that the request is impossible.
  const retry = run.orchestration_mode === "pi" ? "generate again or switch to the DAG workflow" : "generate again";
  const noBuild = failures.includes("error")
    ? "The Pi supervisor could not finish because a model call failed; no recommendation was produced."
    : failures.includes("steps")
      ? `The Pi supervisor stopped before reaching a build that passes every check. This does not mean the request is impossible: ${retry}.`
      : "The Pi supervisor found no compatible build within this budget. Raise the budget or relax a requirement.";
  return {
    run_id: run.id,
    status: "completed",
    outcome: options.length ? "recommendations_available" : "no_feasible_option",
    requirements_version: run.requirements_version,
    snapshot_id: run.snapshot_id,
    orchestration_mode: "pi",
    options,
    assistant_message: options.length
      ? `The Pi supervisor found ${options.length} validated option${options.length === 1 ? "" : "s"}.`
      : `${noBuild} Hard constraints were not relaxed.`,
    generation_source: "llm",
    plan: { active_agents: branches.map((b) => `pi_supervisor:${b}`), tasks: ["find_parts", "check_build", "find_evidence", "submit_build"] },
    agent_trace: trace,
    tool_calls: toolCalls,
    memory_context: { confirmed_memory_ids: [] },
    model_usage: usage,
    limitations,
  };
}

const server = createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/health") {
      reply(response, 200, { status: "ok", runtime: "pi-agent-core", model_provider: "ollama", model: modelId, tools: 6 });
      return;
    }
    if (request.method === "POST" && request.url === "/v1/runs") {
      reply(response, 200, await execute(await readJson(request)));
      return;
    }
    reply(response, 404, { error: "not_found" });
  } catch (error) {
    reply(response, 500, { error: "pi_run_failed", message: error instanceof Error ? error.message : String(error) });
  }
});

server.listen(port, "127.0.0.1", () => {
  console.log(`BuildRig Pi Worker listening on http://127.0.0.1:${port}`);
});
