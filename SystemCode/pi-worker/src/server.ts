import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { Agent } from "@earendil-works/pi-agent-core";
import { createModels, createProvider, type Model } from "@earendil-works/pi-ai";
import { openAICompletionsApi } from "@earendil-works/pi-ai/api/openai-completions.lazy";

type Json = Record<string, any>;

const port = Number(process.env.PI_PORT || 8090);
const apiBase = (process.env.BUILDRIG_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const internalToken = process.env.BUILDRIG_INTERNAL_API_TOKEN || "";
const ollamaBase = (process.env.OLLAMA_BASE_URL || "http://127.0.0.1:11434").replace(/\/$/, "");
const modelId = process.env.BUILDRIG_MODEL || "qwen3:8b";

async function readJson(request: IncomingMessage): Promise<Json> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
}

function reply(response: ServerResponse, status: number, body: Json) {
  response.writeHead(status, { "content-type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(body));
}

async function backend(path: string, init: RequestInit = {}) {
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      "x-internal-token": internalToken,
      ...(init.headers || {}),
    },
  });
  if (!response.ok) throw new Error(`Backend ${path} failed (${response.status}): ${await response.text()}`);
  return response.json() as Promise<Json>;
}

function createPiAgent(runId: string) {
  const model: Model<"openai-completions"> = {
    id: modelId,
    name: `${modelId} (Ollama)`,
    api: "openai-completions",
    provider: "ollama",
    baseUrl: `${ollamaBase}/v1`,
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 8192,
    maxTokens: 1200,
    compat: {
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      supportsStore: false,
    },
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
  return new Agent({
    initialState: {
      systemPrompt: "You are the BuildRig planning agent. Always answer in English. Explain decisions from supplied evidence only. Never invent products, prices, compatibility, benchmarks, or reviews. Return one JSON object and no markdown.",
      model,
      thinkingLevel: "off",
      tools: [],
      messages: [],
    },
    streamFn: models.streamSimple.bind(models),
    getApiKey: async () => "ollama",
    sessionId: runId,
    toolExecution: "parallel",
  });
}

function assistantText(agent: Agent): string {
  const message = [...agent.state.messages].reverse().find((item: any) => item.role === "assistant") as any;
  if (!message) return "";
  if (typeof message.content === "string") return message.content;
  return (message.content || []).filter((part: any) => part.type === "text").map((part: any) => part.text).join("");
}

function parseAgentJson(text: string): Json {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  const candidate = fenced || text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1);
  return JSON.parse(candidate);
}

function optionFromRows(deviceType: string, rows: Json[], requirements: Json): Json {
  const items = rows.map((row) => ({
    category: row.category,
    product_id: String(row.product_id || row.id),
    offer_id: row.id,
    name: row.name,
    quantity: 1,
    price: row.price,
    currency: row.currency,
    merchant: row.store,
    source_url: row.source_url,
    collected_at: row.collected_at,
    availability: row.available ? "in_stock_at_collection" : "unavailable",
  }));
  return {
    option_id: `pi_option_${rows[0]?.id || "empty"}`,
    device_type: deviceType,
    title: "Pi Agent catalogue candidate",
    items,
    required_categories: deviceType === "desktop" ? ["cpu", "motherboard", "ram", "ssd", "gpu", "psu", "case", "cooler"] : ["laptop"],
    reasons: [`Selected from the pinned catalogue within the SGD ${(requirements.budget.maximum_minor / 100).toFixed(2)} budget.`],
    trade_offs: ["Compatibility remains unknown when source specifications are incomplete."],
  };
}

async function execute(payload: Json): Promise<Json> {
  const { run, requirements, maximum_options: maximumOptions = 3 } = payload;
  const budget = requirements.budget.maximum_minor;
  const allocations: Record<string, number> = { cpu: .18, motherboard: .10, ram: .07, ssd: .07, gpu: .36, psu: .07, case: .08, cooler: .07 };
  const categories = requirements.device_type === "desktop" ? Object.keys(allocations) : ["laptop"];
  const candidateGroups = await Promise.all(categories.map(async (category) => {
    const maximum = requirements.device_type === "desktop" ? Math.floor(budget * allocations[category]) : budget;
    const limit = requirements.device_type === "desktop" ? 1 : maximumOptions;
    return (await backend(`/api/v1/internal/candidates/${category}?maximum_minor=${maximum}&limit=${limit}`)).items as Json[];
  }));
  const optionRows = requirements.device_type === "desktop" ? candidateGroups.flat() : candidateGroups[0];
  const options = optionRows.length
    ? (requirements.device_type === "desktop" ? [optionFromRows("desktop", optionRows, requirements)] : optionRows.map((row) => optionFromRows("laptop", [row], requirements)))
    : [];

  await Promise.all(options.map(async (option) => {
    const [evidence, validation] = await Promise.all([
      backend("/api/v1/internal/retrieval/hybrid", { method: "POST", body: JSON.stringify({ run_id: run.id, query: `${(requirements.workloads || []).join(" ")} reliability performance`, snapshot_id: run.snapshot_id, product_ids: option.items.map((item: Json) => item.product_id), categories: [], top_k: 8 }) }),
      backend("/api/v1/internal/options/validate", { method: "POST", body: JSON.stringify({ run_id: run.id, option, requirements }) }),
    ]);
    option.evidence = evidence.items;
    option.validation = validation;
    option.review = { decision: validation.overall_status === "failed" ? "reject" : validation.overall_status === "unknown" ? "accept_with_unknowns" : "accept", notes: [] };
  }));

  const feasible = options.filter((option) => option.validation.overall_status !== "failed");
  let plan: Json = { active_agents: [requirements.device_type === "desktop" ? "desktop_planner" : "laptop_selector", "evidence_agent", "review_agent"], tasks: ["select_candidates", "retrieve_evidence", "validate", "review"], completion: ["budget_checked", "evidence_attributed", "unknowns_exposed"] };
  let assistantMessage = feasible.length
    ? `I found ${feasible.length} evidence-backed option${feasible.length === 1 ? "" : "s"}.`
    : "I could not find a configuration that satisfies every current requirement. Try adjusting the budget or requirements.";
  let generationSource = "fallback";
  if (feasible.length) {
    try {
      const agent = createPiAgent(run.id);
      await agent.prompt(JSON.stringify({ instruction: "Review this candidate set. Return JSON with keys assistant_message, plan, reasons, trade_offs. assistant_message must naturally summarize the result and mention unknown checks. Keep each list concise. Use only supplied facts.", requirements, options: feasible }));
      if (agent.state.errorMessage) throw new Error(agent.state.errorMessage);
      const output = assistantText(agent);
      if (!output.trim()) throw new Error("Pi Agent returned no assistant text");
      const reasoned = parseAgentJson(output);
      if (reasoned.plan && typeof reasoned.plan === "object" && !Array.isArray(reasoned.plan)) {
        plan = { ...plan, ...reasoned.plan };
      } else if (Array.isArray(reasoned.plan)) {
        plan.reasoning_steps = reasoned.plan;
      }
      if (Array.isArray(reasoned.reasons)) feasible[0].reasons = reasoned.reasons;
      if (Array.isArray(reasoned.trade_offs)) feasible[0].trade_offs = reasoned.trade_offs;
      if (typeof reasoned.assistant_message === "string" && reasoned.assistant_message.trim()) assistantMessage = reasoned.assistant_message.trim();
      generationSource = "llm";
    } catch (error) {
      plan = { ...plan, runtime_note: `Pi reasoning fallback used: ${error instanceof Error ? error.message : String(error)}` };
    }
  }
  return {
    run_id: run.id,
    status: "completed",
    outcome: feasible.length ? "recommendations_available" : "no_feasible_option",
    requirements_version: run.requirements_version,
    snapshot_id: run.snapshot_id,
    orchestration_mode: "pi",
    options: feasible,
    assistant_message: assistantMessage,
    generation_source: generationSource,
    plan,
    agent_trace: [
      { agent: "pi_coordinator", task: "plan" },
      { agent: requirements.device_type === "desktop" ? "desktop_planner" : "laptop_selector", task: "select_candidates" },
      { agent: "evidence_agent+review_agent", task: "retrieve_validate" },
    ],
    tool_calls: [],
    memory_context: { confirmed_memory_ids: [] },
    limitations: ["Compatibility remains unknown where the source catalogue lacks required specifications."],
  };
}

const server = createServer(async (request, response) => {
  try {
    if (request.method === "GET" && request.url === "/health") {
      reply(response, 200, { status: "ok", runtime: "pi-agent-core", model_provider: "ollama", model: modelId });
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
