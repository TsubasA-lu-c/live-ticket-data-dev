interface Env {
  AI: {
    run(model: string, input: Record<string, unknown>, options?: Record<string, unknown>): Promise<unknown>;
  };
  AI_MODEL_PRIMARY?: string;
  AI_MODEL_DEBUG_A?: string;
  AI_MODEL_DEBUG_B?: string;
  AI_MODEL_DEBUG_C?: string;
  AI_DEBUG_MODELS_ENABLED?: string;
  LIVE_EXTRACT_SHARED_SECRET?: string;
  AI_GATEWAY_ID?: string;
}
interface LiveExtractRequest {
  artistName: string;
  pageURL: string;
  pageTitle: string;
  snapshotText: string;
  debugModelAlias?: string | null;
}

interface LiveExtractV2Request extends LiveExtractRequest {
  contractVersion: 2;
  extractorVersion: string;
  requestId: string;
  document: { canonicalURL: string; title: string; locale: string; contentHash: string };
  capture: {
    truncated: boolean;
    pagesAttempted: string[];
    pagesSucceeded: string[];
    pagesFailed: string[];
  };
  chunks: Array<{
    chunkId: string;
    blocks: Array<{
      blockId: string;
      pageURL: string;
      sectionPath: string[];
      type: string;
      text: string;
      expectedEvent: boolean;
    }>;
  }>;
  forceRefresh: boolean;
}

type V2Block = LiveExtractV2Request["chunks"][number]["blocks"][number];
interface V2Group { groupId: string; titleText: string; sourceBlockId?: string }
interface V2Performance {
  sourceBlockId: string; groupId: string; dateText: string; regionText: string;
  venueText: string; openTimeText: string; startTimeText: string; evidenceText: string;
}
interface V2Rejection { blockId: string; reason: string }
interface V2ChunkResult {
  groups: V2Group[];
  performances: V2Performance[];
  rejected: V2Rejection[];
  warnings: string[];
  usage?: unknown;
  cacheable?: boolean;
}

const MAX_SNAPSHOT_LENGTH = 60_000;
const MAX_PERFORMANCES = 200;
const MAX_TOTAL_RESULTS = 1_200;
const MAX_BODY_BYTES = 900_000;
const MAX_CHUNKS = 64;
const MAX_BLOCKS = 1_200;
const MAX_BLOCK_TEXT = 30_000;
const MAX_TOTAL_BLOCK_TEXT = 500_000;
const V2_PRIMARY_CONCURRENCY = 2;
const CACHE_TTL_SECONDS = 24 * 60 * 60;
const CACHE_SCHEMA_VERSION = "live-extract-v13";
const V2_SCHEMA_VERSION = "live-extract-contract-v2.7";
const WORKER_BUILD_VERSION = "live-extract-worker-v2.7.1";

const performanceSchema = {
  type: "object",
  additionalProperties: false,
  required: [
    "groupTitleText",
    "dateText",
    "regionText",
    "venueText",
    "openTimeText",
    "startTimeText",
    "kind",
  ],
  properties: {
    groupTitleText: { type: "string" },
    dateText: { type: "string" },
    regionText: { type: "string" },
    venueText: { type: "string" },
    openTimeText: { type: "string" },
    startTimeText: { type: "string" },
    kind: {
      type: "string",
      enum: ["tour", "live", "festival", "event", "standalone", "unknown"],
    },
  },
};

const responseSchema = {
  type: "object",
  additionalProperties: false,
  required: ["performances"],
  properties: {
    performances: {
      type: "array",
      maxItems: MAX_PERFORMANCES,
      items: performanceSchema,
    },
  },
};

const v2ChunkResponseSchema = {
  type: "object",
  additionalProperties: false,
  required: ["performances"],
  properties: {
    performances: { type: "array", maxItems: MAX_PERFORMANCES, items: {
      type: "object", additionalProperties: false,
      required: ["sourceBlockId", "groupTitleText", "dateText", "regionText", "venueText", "openTimeText", "startTimeText"],
      properties: {
        sourceBlockId: { type: "string", maxLength: 128 }, groupTitleText: { type: "string", maxLength: 1_000 },
        dateText: { type: "string", maxLength: 1_000 }, regionText: { type: "string", maxLength: 1_000 },
        venueText: { type: "string", maxLength: 1_000 }, openTimeText: { type: "string", maxLength: 1_000 },
        startTimeText: { type: "string", maxLength: 1_000 },
      },
    } },
  },
};

function json(
  body: unknown,
  status = 200,
  extraHeaders: Record<string, string> = {},
): Response {
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function normalizedCacheText(value: string): string {
  return value.normalize("NFKC").replace(/\s+/g, " ").trim();
}

function canonicalPageURL(value: string): string {
  const url = new URL(value);
  url.hash = "";
  return url.toString();
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

async function cacheRequest(
  request: Request,
  input: LiveExtractRequest,
  model: string,
): Promise<Request> {
  // 本文そのものはキャッシュキーにも保存しない。内容をSHA-256へ変換し、
  // ページ更新・モデル変更・プロンプト変更のどれでも自動的に別結果として扱う。
  const fingerprint = await sha256(
    JSON.stringify({
      version: CACHE_SCHEMA_VERSION,
      model,
      aiPayloadHashes: await Promise.all([0, 1].map((attempt) =>
        sha256(stableJSON(v1AIInput(model, input, attempt))))),
      artistName: normalizedCacheText(input.artistName),
      pageURL: canonicalPageURL(input.pageURL),
      pageTitle: normalizedCacheText(input.pageTitle),
      // Preserve whitespace and line/block boundaries: they are semantic input.
      snapshotText: input.snapshotText.normalize("NFKC"),
    }),
  );
  const keyURL = new URL(request.url);
  keyURL.pathname = `/api/live-extract-cache/${fingerprint}`;
  keyURL.search = "";
  return new Request(keyURL.toString(), { method: "GET" });
}

async function readCachedResult(key: Request): Promise<unknown | null> {
  try {
    const response = await caches.default.match(key);
    if (!response) return null;
    return normalizeAIResult(await response.json());
  } catch {
    // キャッシュは最適化なので、障害時もAI解析そのものは継続する。
    return null;
  }
}

async function storeCachedResult(key: Request, value: unknown): Promise<void> {
  try {
    const response = Response.json(value, {
      headers: {
        "Cache-Control": `public, max-age=${CACHE_TTL_SECONDS}`,
        "Content-Type": "application/json; charset=utf-8",
      },
    });
    await caches.default.put(key, response);
  } catch {
    // AI結果は返し、次回のリクエストで再度キャッシュ保存を試す。
  }
}

function parseRequest(value: unknown): LiveExtractRequest | null {
  if (!value || typeof value !== "object") return null;
  const input = value as Record<string, unknown>;
  if (
    typeof input.artistName !== "string" ||
    typeof input.pageURL !== "string" ||
    typeof input.pageTitle !== "string" ||
    typeof input.snapshotText !== "string"
  ) {
    return null;
  }
  if (
    input.artistName.length === 0 ||
    input.artistName.length > 200 ||
    input.pageURL.length > 2_048 ||
    input.pageTitle.length > 500 ||
    input.snapshotText.length === 0 ||
    input.snapshotText.length > MAX_SNAPSHOT_LENGTH
  ) {
    return null;
  }
  let url: URL;
  try {
    url = new URL(input.pageURL);
  } catch {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (
    input.debugModelAlias !== undefined &&
    input.debugModelAlias !== null &&
    typeof input.debugModelAlias !== "string"
  ) {
    return null;
  }
  return input as unknown as LiveExtractRequest;
}

function validHTTPURL(value: unknown, maxLength = 2_048): value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) return false;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch { return false; }
}

function strings(value: unknown, maxItems: number, maxLength: number): value is string[] {
  return Array.isArray(value) && value.length <= maxItems &&
    value.every((item) => typeof item === "string" && item.length <= maxLength);
}

function onlyKeys(value: Record<string, unknown>, allowed: string[]): boolean {
  const keys = new Set(allowed);
  return Object.keys(value).every((key) => keys.has(key));
}

export function parseV2Request(value: unknown): LiveExtractV2Request | null {
  const legacy = parseRequest(value);
  if (!legacy || !value || typeof value !== "object") return null;
  const input = value as Record<string, unknown>;
  if (!onlyKeys(input, ["artistName", "pageURL", "pageTitle", "snapshotText", "debugModelAlias",
    "contractVersion", "extractorVersion", "requestId", "document", "capture", "chunks", "forceRefresh"])) return null;
  if (input.contractVersion !== 2 || typeof input.extractorVersion !== "string" ||
      input.extractorVersion.length === 0 || input.extractorVersion.length > 100 ||
      typeof input.requestId !== "string" || !/^[A-Za-z0-9._:-]{8,128}$/.test(input.requestId) ||
      (typeof input.debugModelAlias === "string" && input.debugModelAlias.length > 64) ||
      typeof input.forceRefresh !== "boolean") return null;
  const document = input.document as Record<string, unknown> | null;
  if (!document || !onlyKeys(document, ["canonicalURL", "title", "locale", "contentHash"]) ||
      !validHTTPURL(document.canonicalURL) ||
      typeof document.title !== "string" || document.title.length > 500 ||
      typeof document.locale !== "string" || document.locale.length > 40 ||
      typeof document.contentHash !== "string" || !/^[a-f0-9]{64}$/i.test(document.contentHash)) return null;
  const capture = input.capture as Record<string, unknown> | null;
  if (!capture || !onlyKeys(capture, ["truncated", "pagesAttempted", "pagesSucceeded", "pagesFailed"]) ||
      typeof capture.truncated !== "boolean" ||
      !strings(capture.pagesAttempted, 128, 2_048) || !strings(capture.pagesSucceeded, 128, 2_048) ||
      !strings(capture.pagesFailed, 128, 2_048) ||
      ![...capture.pagesAttempted, ...capture.pagesSucceeded, ...capture.pagesFailed].every((url) => validHTTPURL(url))) return null;
  const attempted = new Set(capture.pagesAttempted);
  if (![...capture.pagesSucceeded, ...capture.pagesFailed].every((url) => attempted.has(url)) ||
      capture.pagesSucceeded.some((url) => capture.pagesFailed.includes(url))) return null;
  if (!Array.isArray(input.chunks) || input.chunks.length > MAX_CHUNKS) return null;
  const chunkIds = new Set<string>();
  const blockIds = new Set<string>();
  let blockCount = 0;
  let totalText = 0;
  let totalMetadataText = 0;
  for (const rawChunk of input.chunks) {
    if (!rawChunk || typeof rawChunk !== "object") return null;
    const chunk = rawChunk as Record<string, unknown>;
    if (!onlyKeys(chunk, ["chunkId", "blocks"]) || typeof chunk.chunkId !== "string" ||
        !/^[A-Za-z0-9._:-]{1,128}$/.test(chunk.chunkId) || chunkIds.has(chunk.chunkId)) return null;
    chunkIds.add(chunk.chunkId);
    if (!Array.isArray(chunk.blocks) || chunk.blocks.length === 0 || chunk.blocks.length > MAX_BLOCKS) return null;
    for (const rawBlock of chunk.blocks) {
      if (!rawBlock || typeof rawBlock !== "object") return null;
      const block = rawBlock as Record<string, unknown>;
      if (!onlyKeys(block, ["blockId", "pageURL", "sectionPath", "type", "text", "expectedEvent"]) ||
          typeof block.blockId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(block.blockId) || blockIds.has(block.blockId) ||
          !validHTTPURL(block.pageURL) || !strings(block.sectionPath, 16, 500) ||
          typeof block.type !== "string" || block.type.length === 0 || block.type.length > 80 ||
          typeof block.text !== "string" || block.text.length === 0 || block.text.length > MAX_BLOCK_TEXT ||
          typeof block.expectedEvent !== "boolean") return null;
      blockIds.add(block.blockId);
      blockCount += 1;
      totalText += block.text.length;
      totalMetadataText += block.blockId.length + block.pageURL.length + block.type.length +
        (block.sectionPath as string[]).reduce((sum, item) => sum + item.length, 0);
      if (blockCount > MAX_BLOCKS || totalText > MAX_TOTAL_BLOCK_TEXT || totalMetadataText > 300_000) return null;
    }
  }
  return value as LiveExtractV2Request;
}

function selectModel(env: Env, alias: string | null | undefined): string | null {
  if (!alias || env.AI_DEBUG_MODELS_ENABLED !== "true") {
    return env.AI_MODEL_PRIMARY ?? null;
  }
  const debugModels: Record<string, string | undefined> = {
    primary: env.AI_MODEL_PRIMARY,
    a: env.AI_MODEL_DEBUG_A,
    b: env.AI_MODEL_DEBUG_B,
    c: env.AI_MODEL_DEBUG_C,
  };
  return debugModels[alias] ?? null;
}

function isPaidRequiredModel(model: string): boolean {
  const value = model.toLowerCase();
  return value.includes("kimi") || value.includes("moonshot") || value.includes("glm-5") ||
    value.includes("glm5");
}

function isWorkersAIModel(model: string): boolean {
  return model.startsWith("@cf/");
}

function isGLMModel(model: string): boolean {
  const modelName = model.toLowerCase().split("/").at(-1) ?? "";
  return /^glm(?:[-_.]|$)/.test(modelName);
}

function modelGenerationSettings(
  model: string, responseFormat: Record<string, unknown>, maxTokens: number,
): Record<string, unknown> {
  const common = { response_format: responseFormat, temperature: 0 };
  if (isGLMModel(model)) {
    return {
      ...common,
      max_completion_tokens: maxTokens,
      chat_template_kwargs: { enable_thinking: false },
    };
  }
  return { ...common, max_tokens: maxTokens };
}

function stableJSON(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJSON).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value as Record<string, unknown>).sort().map((key) =>
      `${JSON.stringify(key)}:${stableJSON((value as Record<string, unknown>)[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function v2Prompt(input: LiveExtractV2Request, blocks: V2Block[]): string {
  return `日本の公式ライブ情報を、次のブロック本文だけから抽出してください。\n` +
    `expectedEvent=trueの各event blockから公演を抽出し、sourceBlockIdを必ず維持してください。` +
    `文字列はすべてそのevent block本文のexact substringだけを返し、別ブロックの文字列を混ぜず、不明は空文字にしてください。` +
    `groupTitleTextは公演名を年・副題・括弧書き・引用符まで省略せず、本文にある正式名のexact substringで返してください。` +
    `行頭で日付より前に単独表示されるFES/EVENT/TOUR/LIVEだけはカテゴリなので除外しますが、公演名に含まれるLive Tour等の語は残してください。` +
    `NEWS投稿日や受付日を公演日にしません。同じsourceBlockIdを重複して返さないでください。\n` +
    `artist=${input.artistName}\nlocale=${input.document.locale}\nblocks=\n` +
    blocks.map((block) => stableJSON({ blockId: block.blockId, pageURL: block.pageURL,
      sectionPath: block.sectionPath, type: block.type, text: block.text,
      expectedEvent: block.expectedEvent })).join("\n");
}

function v2MaxTokens(chunk: LiveExtractV2Request["chunks"][number]): number {
  const expected = chunk.blocks.filter((block) => block.expectedEvent).length;
  return Math.min(8_000, Math.max(900, 500 + expected * 240 + chunk.blocks.length * 35));
}

function v2AIInput(
  model: string, input: LiveExtractV2Request,
  chunk: LiveExtractV2Request["chunks"][number], attempt: number,
): Record<string, unknown> {
  return {
    messages: [
      { role: "system", content: attempt === 0
        ? "本文のexact substringだけを根拠に、指定JSON Schema以外を返さないでください。"
        : "JSONオブジェクトだけを返してください。top-levelのperformances配列にsourceBlockId,groupTitleText,dateText,regionText,venueText,openTimeText,startTimeTextを持つ要素を入れてください。" },
      { role: "user", content: v2Prompt(input, chunk.blocks) },
    ],
    ...modelGenerationSettings(
      model,
      attempt === 0
        ? { type: "json_schema", json_schema: v2ChunkResponseSchema }
        : { type: "json_object" },
      v2MaxTokens(chunk),
    ),
  };
}

function parseStructuredText(text: string): unknown {
  const trimmed = text.trim();
  const fenced = trimmed.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/i)?.[1] ?? trimmed;
  try { return JSON.parse(fenced); } catch {
    // Some chat-style models prefix a short explanation even in JSON mode.
    // Semantic validation below still rejects invented or cross-block values.
    const start = fenced.indexOf("{");
    const end = fenced.lastIndexOf("}");
    if (start >= 0 && end > start) {
      try { return JSON.parse(fenced.slice(start, end + 1)); } catch { /* invalid */ }
    }
    return null;
  }
}

function contentValue(value: unknown): unknown {
  if (!Array.isArray(value)) return value;
  const parts = value.flatMap((item): string[] => {
    if (typeof item === "string") return [item];
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    if (typeof record.text === "string") return [record.text];
    if (typeof record.content === "string") return [record.content];
    return [];
  });
  return parts.length ? parts.join("") : value;
}

function valueShape(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return `array(${value.length})`;
  if (typeof value === "string") return `string(${value.length})`;
  if (typeof value !== "object") return typeof value;
  return `object(${Object.keys(value as Record<string, unknown>).sort().slice(0, 12).join(",")})`;
}

function aiResultShape(value: unknown): string {
  const parts = [`raw:${valueShape(value)}`];
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const raw = value as Record<string, unknown>;
    if (raw.response !== undefined) parts.push(`response:${valueShape(raw.response)}`);
    if (raw.result !== undefined) parts.push(`result:${valueShape(raw.result)}`);
    const choice = Array.isArray(raw.choices) ? raw.choices[0] : undefined;
    if (choice !== undefined) {
      parts.push(`choice:${valueShape(choice)}`);
      if (choice && typeof choice === "object") {
        const record = choice as Record<string, unknown>;
        parts.push(`message:${valueShape(record.message)}`);
        if (record.message && typeof record.message === "object") {
          const message = record.message as Record<string, unknown>;
          parts.push(`content:${valueShape(message.content)}`);
          parts.push(`parsed:${valueShape(message.parsed)}`);
        }
      }
    }
  }
  const unwrapped = unwrapAIResult(value);
  parts.push(`unwrapped:${valueShape(unwrapped.value)}`);
  parts.push(`finish:${unwrapped.finishReason ?? "none"}`);
  return parts.join(";").slice(0, 700);
}

function invalidAIResponse(value: unknown): Error {
  const error = new Error("invalid_ai_response") as Error & { diagnostic?: string };
  error.diagnostic = aiResultShape(value);
  return error;
}

function unwrapAIResult(value: unknown): { value: unknown; usage?: unknown; finishReason?: string } {
  let usage: unknown;
  let finishReason: string | undefined;
  for (let depth = 0; depth < 8; depth += 1) {
    if (typeof value === "string") {
      value = parseStructuredText(value);
      if (value === null) break;
      continue;
    }
    if (Array.isArray(value)) {
      const next = contentValue(value);
      if (next === value) break;
      value = next;
      continue;
    }
    if (!value || typeof value !== "object") break;
    const envelope = value as Record<string, unknown>;
    if (usage === undefined && envelope.usage !== undefined) usage = envelope.usage;
    const envelopeFinish = typeof envelope.finish_reason === "string" ? envelope.finish_reason :
      typeof envelope.finishReason === "string" ? envelope.finishReason : undefined;
    if (envelopeFinish) finishReason = envelopeFinish;
    if ("response" in envelope) {
      value = envelope.response;
      continue;
    }
    if ("result" in envelope && Object.keys(envelope).every((key) =>
      ["result", "usage", "finish_reason", "finishReason"].includes(key))) {
      value = envelope.result;
      continue;
    }
    if (Array.isArray(envelope.choices) && envelope.choices.length > 0 &&
        envelope.choices[0] && typeof envelope.choices[0] === "object") {
      const choice = envelope.choices[0] as Record<string, unknown>;
      const choiceFinish = typeof choice.finish_reason === "string" ? choice.finish_reason :
        typeof choice.finishReason === "string" ? choice.finishReason : undefined;
      if (choiceFinish) finishReason = choiceFinish;
      const message = choice.message && typeof choice.message === "object"
        ? choice.message as Record<string, unknown> : undefined;
      if (message && message.parsed !== undefined) value = message.parsed;
      else if (message && message.content !== undefined) value = contentValue(message.content);
      else if (choice.text !== undefined) value = choice.text;
      else value = null;
      continue;
    }
    if (envelope.output_text !== undefined) {
      value = envelope.output_text;
      continue;
    }
    break;
  }
  return { value, usage, finishReason };
}

function isParseableDate(value: string): boolean {
  if (!value) return false;
  const iso = value.match(/(20\d{2})[-\/.](\d{1,2})[-\/.](\d{1,2})/);
  const jp = value.match(/(?:(20\d{2})年)?\s*(\d{1,2})月\s*(\d{1,2})日/);
  const short = value.match(/(?:^|\D)(\d{1,2})[\/.](\d{1,2})(?:\D|$)/);
  const match = iso ?? jp;
  if (!match) {
    // Omitted-year rows are valid source evidence, but the Worker must not guess
    // a year. The iOS review layer keeps it unresolved until deterministic page
    // context or the user supplies one.
    if (!short) return false;
    const month = Number(short[1]), day = Number(short[2]);
    return month >= 1 && month <= 12 && day >= 1 &&
      day <= new Date(Date.UTC(2000, month, 0)).getUTCDate();
  }
  const year = Number(match[1] || 2000), month = Number(match[2]), day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
}

interface CanonicalSourceToken { value: string; start: number; end: number }

function canonicalSourceTokens(value: string): CanonicalSourceToken[] {
  const tokens: CanonicalSourceToken[] = [];
  const quotes = new Set(["\"", "“", "„", "‟", "”", "「", "」", "『", "』", "｢", "｣"]);
  const apostrophes = new Set(["'", "’", "‘", "‛", "＇", "`", "´"]);
  const openings = new Map([["(", "("], ["[", "["], ["{", "{"], ["〈", "<"], ["《", "<"], ["【", "["]]);
  const closings = new Map([[")", ")"], ["]", "]"], ["}", "}"], ["〉", ">"], ["》", ">"], ["】", "]"]]);
  let offset = 0;
  for (const original of value) {
    const start = offset;
    offset += original.length;
    for (const normalized of original.normalize("NFKC")) {
      let canonical = normalized;
      // Whitespace is formatting, not evidence. Ignoring it lets us restore the exact source
      // spelling (including its original spaces) without accepting changed words.
      if (/\s/u.test(normalized)) continue;
      if (quotes.has(normalized)) canonical = "\"";
      else if (apostrophes.has(normalized)) canonical = "'";
      else canonical = openings.get(normalized) ?? closings.get(normalized) ?? normalized;
      tokens.push({ value: canonical, start, end: offset });
    }
  }
  return tokens;
}

/** Returns the exact source spelling when only Unicode width/space/quote style differs. */
function resolveSourceSubstring(source: string, candidate: string): string | null {
  if (!candidate) return "";
  if (source.includes(candidate)) return candidate;
  const sourceTokens = canonicalSourceTokens(source);
  const candidateTokens = canonicalSourceTokens(candidate);
  if (candidateTokens.length === 0 || candidateTokens.length > sourceTokens.length) return null;
  outer: for (let start = 0; start <= sourceTokens.length - candidateTokens.length; start += 1) {
    for (let index = 0; index < candidateTokens.length; index += 1) {
      if (sourceTokens[start + index].value !== candidateTokens[index].value) continue outer;
    }
    const first = sourceTokens[start];
    const last = sourceTokens[start + candidateTokens.length - 1];
    return source.slice(first.start, last.end);
  }
  return null;
}

/**
 * Expands an already-grounded model title to the complete source segment between
 * the grounded date and grounded location. This is deliberately bounded to one
 * event block and only applies when the model title itself occurs inside that
 * segment, so it cannot borrow a tour name from another performance.
 */
function completeGroundedTitle(
  source: string, title: string, dateText: string, regionText: string, venueText: string,
): string {
  if (!title || !dateText || !regionText || !venueText) return title;
  const dateStart = source.indexOf(dateText);
  const titleStart = source.indexOf(title, Math.max(0, dateStart));
  if (dateStart < 0 || titleStart < dateStart) return title;

  // The candidate must be the first semantic text after the grounded date and
  // an optional weekday. Otherwise expanding it could absorb an artist/status
  // label which the model intentionally excluded.
  const betweenDateAndTitle = source.slice(dateStart + dateText.length, titleStart);
  const prefixWithoutWeekday = betweenDateAndTitle
    .replace(/^\s*[（(]\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?|[月火水木金土日](?:曜(?:日)?)?)\s*[)）]/iu, "")
    .trim();
  if (prefixWithoutWeekday) return title;

  const venueStart = source.indexOf(venueText, titleStart + title.length);
  if (venueStart < 0) return title;
  // Use the last region occurrence before the grounded venue. A region word may
  // legitimately occur inside a title; the location label is the one nearest
  // the venue.
  const regionStart = source.lastIndexOf(regionText, venueStart - 1);
  if (regionStart < titleStart + title.length || regionStart >= venueStart) return title;
  const segment = source.slice(titleStart, regionStart)
    // A region commonly begins with a bracket immediately before the value.
    .replace(/[\s\u00a0　]*[【\[(（〈《「『｢]\s*$/u, "")
    .trim();
  const extension = segment.slice(title.length);
  if (!segment.startsWith(title) || segment.length > 300 ||
      /(?:20\d{2}[\/.年-]\d{1,2}|\bOPEN\b|\bSTART\b|開場|開演|受付|発売|問い合わせ|問合せ)/iu.test(extension)) return title;
  return segment;
}

function titleLikeText(value: string): boolean {
  return value.length > 0 && value.length <= 1_000 &&
    /(?:LIVE|TOUR|CONCERT|FESTIVAL|FES|EVENT|ライブ|ツアー|コンサート|フェス)/iu.test(value) &&
    !isParseableDate(value);
}

function titleLikeHeading(block: V2Block): boolean {
  return !block.expectedEvent && /heading|title/i.test(block.type) && titleLikeText(block.text);
}

interface GroundedGroupTitle { titleText: string; sourceBlockId?: string }

/** The row's own DOM heading path is stronger context than a page-wide search. */
function fallbackGroupTitle(block: V2Block, blocks: V2Block[]): GroundedGroupTitle | undefined {
  const pathTitle = [...block.sectionPath].reverse().find(titleLikeText);
  if (pathTitle) {
    const heading = blocks.find((candidate) =>
      candidate.pageURL === block.pageURL && titleLikeHeading(candidate) &&
      candidate.text === pathTitle);
    return { titleText: pathTitle, ...(heading ? { sourceBlockId: heading.blockId } : {}) };
  }

  // Some sites expose a page-level title without attaching it to each row. Only
  // use it when exactly one official title-like heading exists on that page.
  const candidates = blocks.filter((candidate) =>
    candidate.pageURL === block.pageURL && titleLikeHeading(candidate));
  return candidates.length === 1
    ? { titleText: candidates[0].text, sourceBlockId: candidates[0].blockId }
    : undefined;
}

export function validateV2ChunkResult(value: unknown, blocks: V2Block[]): V2ChunkResult | null {
  const parsed = unwrapAIResult(value);
  if (!parsed.value || typeof parsed.value !== "object" || parsed.finishReason === "length") return null;
  const raw = parsed.value as Record<string, unknown>;
  if (!Array.isArray(raw.performances) || raw.performances.length > MAX_PERFORMANCES) return null;
  const byId = new Map(blocks.map((block) => [block.blockId, block]));
  const groups: V2Group[] = [];
  const groupIdsByKey = new Map<string, string>();
  const warnings: string[] = [];
  const rejected: V2Rejection[] = [];
  let cacheable = true;
  const performances: V2Performance[] = [];
  const coveredBlockIds = new Set<string>();
  const requiredFields = ["dateText"] as const;
  const optionalFields = ["regionText", "venueText", "openTimeText", "startTimeText"] as const;
  const fields = [...requiredFields, ...optionalFields] as const;
  for (const item of raw.performances) {
    if (!item || typeof item !== "object") { warnings.push("invalid performance object"); cacheable = false; continue; }
    const performance = item as Record<string, unknown>;
    const blockId = typeof performance.sourceBlockId === "string" ? performance.sourceBlockId : "";
    const block = byId.get(blockId);
    if (!block) {
      warnings.push(`performance: unknown sourceBlockId${blockId ? ` ${blockId}` : ""}`);
      cacheable = false;
      continue;
    }
    const fail = (reason: string) => { rejected.push({ blockId, reason }); cacheable = false; };
    if (!block.expectedEvent) { fail("sourceBlockId is not an event block"); continue; }
    if (coveredBlockIds.has(blockId)) { fail("duplicate sourceBlockId"); continue; }
    if (typeof performance.groupTitleText !== "string" || performance.groupTitleText.length > 1_000) {
      fail("invalid groupTitleText"); continue;
    }
    if (!fields.every((field) => typeof performance[field] === "string" && (performance[field] as string).length <= 2_000)) { fail("invalid performance fields"); continue; }
    const groundedFields = Object.fromEntries(fields.map((field) => [
      field, resolveSourceSubstring(block.text, performance[field] as string),
    ])) as Record<(typeof fields)[number], string | null>;
    if (requiredFields.some((field) => groundedFields[field] === null)) {
      fail("required field is not exact source substring");
      continue;
    }
    for (const field of optionalFields) {
      if (groundedFields[field] === null) {
        groundedFields[field] = "";
        warnings.push(`${blockId}: ${field} is not source-grounded; field cleared`);
        cacheable = false;
      }
    }
    if (!isParseableDate(groundedFields.dateText ?? "")) { fail("dateText is not parseable"); continue; }

    let titleText = "";
    let titleSourceBlockId: string | undefined;
    let ungroundedCandidate = false;
    const candidate = performance.groupTitleText as string;
    if (candidate) {
      // A title must be grounded in the same event block. Looking in an arbitrary nearby
      // heading could associate a schedule row with the wrong tour.
      const resolved = resolveSourceSubstring(block.text, candidate);
      if (resolved !== null) {
        titleText = completeGroundedTitle(
          block.text, resolved, groundedFields.dateText ?? "",
          groundedFields.regionText ?? "", groundedFields.venueText ?? "",
        );
        titleSourceBlockId = block.blockId;
      }
      ungroundedCandidate = !titleText;
    }
    if (!titleText) {
      const heading = fallbackGroupTitle(block, blocks);
      if (heading) {
        titleText = heading.titleText;
        titleSourceBlockId = heading.sourceBlockId;
      }
    }
    // A model title that is not present in the row is harmless when the row's
    // official DOM heading path deterministically replaces it. Warn and avoid
    // caching only when no grounded fallback exists.
    if (!titleText && ungroundedCandidate) {
      warnings.push(`${blockId}: groupTitleText is not source-grounded; title cleared`);
      cacheable = false;
    }
    const groupKey = stableJSON(titleText ? { titleText } : { eventBlockId: blockId });
    let groupId = groupIdsByKey.get(groupKey);
    if (!groupId) {
      groupId = `local-group-${groups.length + 1}`;
      groupIdsByKey.set(groupKey, groupId);
      groups.push({ groupId, titleText, ...(titleSourceBlockId ? { sourceBlockId: titleSourceBlockId } : {}) });
    }
    coveredBlockIds.add(blockId);
    performances.push({
      sourceBlockId: blockId,
      groupId,
      dateText: groundedFields.dateText ?? "",
      regionText: groundedFields.regionText ?? "",
      venueText: groundedFields.venueText ?? "",
      openTimeText: groundedFields.openTimeText ?? "",
      startTimeText: groundedFields.startTimeText ?? "",
      // Date is already exact and parseable, so it is a compact deterministic
      // evidence substring. Asking the model to copy the row doubled output size.
      evidenceText: groundedFields.dateText ?? "",
    });
  }
  if (blocks.some((block) => block.expectedEvent && !coveredBlockIds.has(block.blockId))) cacheable = false;
  return { groups, performances, rejected, warnings, usage: parsed.usage, cacheable };
}

function prompt(input: LiveExtractRequest): string {
  return `あなたは日本の公式アーティストサイトから公演情報を抽出する処理です。
以下の表示済みページ本文だけを根拠に、JSON Schemaどおり返してください。

厳守:
- 本文に存在するライブ、コンサート、ツアー、フェス、イベントだけを抽出する
- NEWS投稿日、チケット受付期間、CD・配信リリース日を公演日にしない
- TV、RADIO、WEB、MAGAZINE、MEDIA、RELEASEを公演扱いしない
- 存在しない公演や不明な日付、会場、時刻を推測・補完しない
- 不明項目は空文字にする
- 翻訳、ローマ字化、要約、言い換えをしない
- 文字列フィールドは本文に存在する原文表記をそのまま返す
- 同じ公演を重複して返さない
- groupTitleTextは各日程に共通するツアー・ライブ・イベント名。ページタイトル、見出し、画像のaltにある公演名も確認し、各公演へ同じ原文を入れる
- 行頭のFES、EVENT、TOUR、LIVEはカテゴリ名であり公演名ではない。「FES 2026」のようにカテゴリと日付年を連結せず、日付の後ろにある固有の公演名をgroupTitleTextへ入れる
- regionTextは都道府県・地域名だけを入れる
- venueTextは会場名だけを入れ、都道府県、(問)以降の問い合わせ先、電話番号、メールアドレス、URLを含めない
- OPEN/START、地域、会場、問い合わせ先が連続していても、それぞれを別フィールドへ分離する
- 時刻が「17:30 / 18:30」のようにスラッシュ区切りの場合、左をopenTimeText、右をstartTimeTextへ入れる
- [SCHEDULE ROW]は公演一覧の1行を示す。同じSCHEDULE内の全行を漏れなく抽出し、途中の日程や後半の日程を省略しない
- [SCHEDULE ROW]を公演情報の正本として扱う。NEWS、受付、当日券などに再掲された同一日付を理由にSCHEDULE ROWを省略・置換しない
- NEWSや本文に中止・延期の記載があっても、SCHEDULEに残っている公演行は推測で除外せず抽出する。保存するかは利用者が確認する

アーティスト名: ${input.artistName}
ページURL（識別用。取得してはいけない）: ${input.pageURL}
ページタイトル: ${input.pageTitle}

表示済みページ本文:
${input.snapshotText}`;
}

function v1AIInput(
  model: string, input: LiveExtractRequest, attempt: number,
): Record<string, unknown> {
  return {
    messages: [
      {
        role: "system",
        content: attempt === 0
          ? "入力本文の文字列だけを根拠に抽出し、指定JSON Schema以外を返さないでください。"
          : "JSONオブジェクトだけを返してください。top-levelのperformances配列に、groupTitleText,dateText,regionText,venueText,openTimeText,startTimeText,kindを持つ要素を入れてください。",
      },
      { role: "user", content: prompt(input) },
    ],
    ...modelGenerationSettings(
      model,
      attempt === 0
        ? { type: "json_schema", json_schema: responseSchema }
        : { type: "json_object" },
      8_000,
    ),
  };
}

export function normalizeAIResult(value: unknown): unknown {
  const parsed = unwrapAIResult(value);
  if (parsed.finishReason === "length") return null;
  value = parsed.value;
  if (!value || typeof value !== "object") return null;
  const performances = (value as { performances?: unknown }).performances;
  if (!Array.isArray(performances) || performances.length > MAX_PERFORMANCES) return null;

  const fields = [
    "groupTitleText",
    "dateText",
    "regionText",
    "venueText",
    "openTimeText",
    "startTimeText",
    "kind",
  ] as const;
  const allowedKinds = new Set([
    "tour",
    "live",
    "festival",
    "event",
    "standalone",
    "unknown",
  ]);
  const safe = performances.filter((item): item is Record<string, string> => {
    if (!item || typeof item !== "object") return false;
    const record = item as Record<string, unknown>;
    if (!fields.every((field) => typeof record[field] === "string")) return false;
    if (!allowedKinds.has(record.kind as string)) return false;
    return fields.every((field) => (record[field] as string).length <= 1_000);
  });
  return { performances: safe };
}

async function v2ChunkCacheRequest(
  request: Request, input: LiveExtractV2Request, chunk: LiveExtractV2Request["chunks"][number], model: string,
): Promise<Request> {
  const aiPayloadHash = await sha256(stableJSON(v2AIInput(model, input, chunk, 0)));
  const fingerprint = await sha256(stableJSON({
    version: V2_SCHEMA_VERSION, contractVersion: 2, extractorVersion: input.extractorVersion,
    model, aiPayloadHash, locale: input.document.locale, chunk,
  }));
  const keyURL = new URL(request.url);
  keyURL.pathname = `/api/live-extract-cache/v2/${fingerprint}`;
  keyURL.search = "";
  return new Request(keyURL.toString(), { method: "GET" });
}

async function contentHash(input: LiveExtractV2Request): Promise<string> {
  const content = input.chunks.flatMap((chunk) => chunk.blocks).map((block) =>
    [block.blockId, block.pageURL, block.sectionPath.join(" > "), block.type, block.text].join("\u001f"),
  ).join("\u001e");
  return sha256(content);
}

async function readCachedV2(key: Request, blocks: V2Block[]): Promise<V2ChunkResult | null> {
  try {
    const response = await caches.default.match(key);
    return response ? validateV2ChunkResult(await response.json(), blocks) : null;
  } catch { return null; }
}

function errorText(error: unknown): string {
  if (error && typeof error === "object") {
    const record = error as Record<string, unknown>;
    return `${record.code ?? ""} ${record.message ?? ""} ${errorText(record.cause)} ${errorText(record.error)}`.toLowerCase();
  }
  return String(error).toLowerCase();
}

function nextUTCDate(): string {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)).toISOString();
}

export async function boundedMapOrdered<Input, Output>(
  inputs: Input[], concurrency: number, transform: (input: Input, index: number) => Promise<Output>,
): Promise<Output[]> {
  if (inputs.length === 0) return [];
  const results = new Array<Output>(inputs.length);
  const workerCount = Math.max(1, Math.min(Math.floor(concurrency), inputs.length));
  let nextIndex = 0;
  let stopped = false;
  let firstError: unknown;

  const worker = async (): Promise<void> => {
    while (!stopped) {
      // JavaScript runs this claim without an await, so two workers cannot claim
      // the same chunk. Once another worker reports quota/failure, no new claim occurs.
      const index = nextIndex;
      if (index >= inputs.length) return;
      nextIndex += 1;
      try {
        results[index] = await transform(inputs[index], index);
      } catch (error) {
        if (firstError === undefined) firstError = error;
        stopped = true;
      }
    }
  };

  await Promise.all(Array.from({ length: workerCount }, () => worker()));
  if (firstError !== undefined) throw firstError;
  return results;
}

async function runV2Chunk(
  request: Request, env: Env, input: LiveExtractV2Request,
  chunk: LiveExtractV2Request["chunks"][number], model: string,
): Promise<{ result: V2ChunkResult; cache: "HIT" | "MISS"; durationMs: number; aiCalls: number }> {
  const startedAt = Date.now();
  const key = await v2ChunkCacheRequest(request, input, chunk, model);
  if (!input.forceRefresh) {
    const cached = await readCachedV2(key, chunk.blocks);
    if (cached) return { result: cached, cache: "HIT", durationMs: Date.now() - startedAt, aiCalls: 0 };
  }
  const result = await env.AI.run(
    model,
    v2AIInput(model, input, chunk, 0),
    env.AI_GATEWAY_ID ? { gateway: { id: env.AI_GATEWAY_ID } } : undefined,
  );
  const validated = validateV2ChunkResult(result, chunk.blocks);
  if (!validated) throw invalidAIResponse(result);
  if (validated.cacheable) {
    await storeCachedResult(key, { performances: validated.performances.map((performance) => {
      const group = validated.groups.find((item) => item.groupId === performance.groupId);
      return {
        sourceBlockId: performance.sourceBlockId,
        groupTitleText: group?.titleText ?? "",
        dateText: performance.dateText,
        regionText: performance.regionText,
        venueText: performance.venueText,
        openTimeText: performance.openTimeText,
        startTimeText: performance.startTimeText,
      };
    }) });
  }
  return { result: validated, cache: "MISS", durationMs: Date.now() - startedAt, aiCalls: 1 };
}

async function onRequestV2(request: Request, env: Env, input: LiveExtractV2Request, model: string): Promise<Response> {
  const startedAt = Date.now();
  if (await contentHash(input) !== input.document.contentHash.toLowerCase()) {
    return json({ code: "invalid_request", message: "Invalid content hash" }, 400, { "X-Request-ID": input.requestId });
  }
  const outcomes: Array<{
    result: V2ChunkResult; cache: "HIT" | "MISS"; chunkId: string; durationMs: number; aiCalls: number;
  }> = [];
  try {
    const primaryOutcomes = await boundedMapOrdered(
      input.chunks, V2_PRIMARY_CONCURRENCY, async (chunk) => {
        const outcome = await runV2Chunk(request, env, input, chunk, model);
        return { ...outcome, chunkId: chunk.chunkId };
      },
    );
    outcomes.push(...primaryOutcomes);
  } catch (error) {
    const message = errorText(error);
    if (message.includes("3036") || message.includes("daily free allocation")) {
      return json({ code: "ai_quota_exhausted", message: "AI is temporarily unavailable", retryAt: nextUTCDate() }, 503);
    }
    if (message.includes("invalid_ai_response")) {
      const diagnostic = error && typeof error === "object" &&
        typeof (error as Record<string, unknown>).diagnostic === "string"
        ? (error as Record<string, string>).diagnostic : undefined;
      return json({ code: "invalid_ai_response", message: "Invalid AI response" }, 502, {
        "X-Live-Extract-Version": WORKER_BUILD_VERSION,
        "X-Request-ID": input.requestId,
        ...(diagnostic ? { "X-AI-Response-Shape": diagnostic } : {}),
      });
    }
    return json({ code: "ai_unavailable", message: "AI is temporarily unavailable" }, 503);
  }

  const allBlocks = input.chunks.flatMap((chunk) => chunk.blocks);
  const allBlockIds = new Set(allBlocks.map((block) => block.blockId));
  const expectedBlockIds = allBlocks.filter((block) => block.expectedEvent).map((block) => block.blockId);
  const groups: V2Group[] = [];
  const groupsByStableId = new Map<string, V2Group>();
  const performances: V2Performance[] = [];
  const rejected: V2Rejection[] = [];
  const warnings: string[] = [];
  const usage: unknown[] = [];
  for (const outcome of outcomes) {
    const idMap = new Map<string, string>();
    for (const group of outcome.result.groups) {
      const stableId = `g-${(await sha256(stableJSON({
        sourceBlockId: group.sourceBlockId ?? "", titleText: group.titleText,
        chunkScope: group.sourceBlockId ? "" : outcome.chunkId,
      }))).slice(0, 20)}`;
      idMap.set(group.groupId, stableId);
      if (!groupsByStableId.has(stableId)) {
        const normalizedGroup = { ...group, groupId: stableId };
        groupsByStableId.set(stableId, normalizedGroup);
        groups.push(normalizedGroup);
      }
    }
    for (const performance of outcome.result.performances) {
      const groupId = idMap.get(performance.groupId);
      if (!groupId) {
        rejected.push({ blockId: performance.sourceBlockId, reason: "group missing during aggregation" });
      } else performances.push({ ...performance, groupId });
    }
    rejected.push(...outcome.result.rejected);
    warnings.push(...outcome.result.warnings.map((warning) => `${outcome.chunkId}: ${warning}`));
    if (outcome.result.usage !== undefined) usage.push(outcome.result.usage);
  }
  if (groups.length > MAX_TOTAL_RESULTS || performances.length > MAX_TOTAL_RESULTS || rejected.length > MAX_TOTAL_RESULTS) {
    return json({ code: "invalid_ai_response", message: "Invalid AI response" }, 502, { "X-Request-ID": input.requestId });
  }
  const coveredSet = new Set(performances.map((performance) => performance.sourceBlockId));
  const coveredBlockIds = expectedBlockIds.filter((id) => coveredSet.has(id));
  // Rejection explains why a block was not accepted; it does not mean the
  // expected performance was covered. Keep it visible in both collections so
  // the client cannot mistake a rejected row for a complete extraction.
  const uncoveredBlockIds = expectedBlockIds.filter((id) => !coveredSet.has(id));
  if (input.capture.truncated) warnings.push("capture was truncated");
  if (uncoveredBlockIds.length) warnings.push(`${uncoveredBlockIds.length} expected event block(s) remain uncovered`);
  const hits = outcomes.filter((outcome) => outcome.cache === "HIT").length;
  const aiCalls = outcomes.reduce((sum, outcome) => sum + outcome.aiCalls, 0);
  const chunkDurationMs = outcomes.reduce((maximum, outcome) => Math.max(maximum, outcome.durationMs), 0);
  const cache = outcomes.length === 0 ? "MISS" : hits === outcomes.length ? "HIT" : hits === 0 ? "MISS" : "MIXED";
  const seenRejections = new Set<string>();
  const finalRejected = rejected.filter((item) => {
    if (!allBlockIds.has(item.blockId) || coveredSet.has(item.blockId)) return false;
    const key = `${item.blockId}\u001f${item.reason}`;
    if (seenRejections.has(key)) return false;
    seenRejections.add(key);
    return true;
  });
  return json({
    contractVersion: 2, groups, performances,
    coverage: { expectedBlockIds, coveredBlockIds, uncoveredBlockIds, rejected: finalRejected },
    warnings, model, ...(usage.length ? { usage: { chunks: usage } } : {}),
  }, 200, {
    "X-Live-Extract-Cache": cache,
    "X-Live-Extract-Version": WORKER_BUILD_VERSION,
    "X-Live-Extract-AI-Calls": String(aiCalls),
    "X-Request-ID": input.requestId,
    "Server-Timing": `chunks;dur=${chunkDurationMs}, total;dur=${Date.now() - startedAt}`,
  });
}

export async function onRequest(context: {
  request: Request;
  env: Env;
}): Promise<Response> {
  const { request, env } = context;
  if (request.method !== "POST") {
    return json({ code: "method_not_allowed", message: "POST required" }, 405);
  }
  const contentType = request.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    return json({ code: "invalid_content_type", message: "JSON required" }, 415);
  }
  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(contentLength) && contentLength > MAX_BODY_BYTES) {
    return json({ code: "request_too_large", message: "Request body too large" }, 413);
  }
  if (env.LIVE_EXTRACT_SHARED_SECRET) {
    const supplied = request.headers.get("x-live-extract-key") ??
      request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
    if (supplied !== env.LIVE_EXTRACT_SHARED_SECRET) {
      return json({ code: "unauthorized", message: "Unauthorized" }, 401);
    }
  }

  let body: unknown;
  try {
    const rawBody = await request.text();
    if (new TextEncoder().encode(rawBody).byteLength > MAX_BODY_BYTES) {
      return json({ code: "request_too_large", message: "Request body too large" }, 413);
    }
    body = JSON.parse(rawBody);
  } catch {
    return json({ code: "invalid_request", message: "Invalid JSON" }, 400);
  }
  const input = body && typeof body === "object" && (body as Record<string, unknown>).contractVersion === 2
    ? parseV2Request(body) : parseRequest(body);
  if (!input) {
    return json({ code: "invalid_request", message: "Invalid request" }, 400);
  }
  const model = selectModel(env, input.debugModelAlias);
  if (!model) {
    return json(
      { code: "ai_unavailable", message: "AI is temporarily unavailable" },
      503,
    );
  }
  if (isPaidRequiredModel(model)) {
    return json({ code: "ai_unavailable", message: "AI is temporarily unavailable" }, 503);
  }
  if (!isWorkersAIModel(model)) {
    return json({ code: "ai_unavailable", message: "AI is temporarily unavailable" }, 503);
  }
  if ((input as Partial<LiveExtractV2Request>).contractVersion === 2) {
    return onRequestV2(request, env, input as LiveExtractV2Request, model);
  }

  try {
    const key = await cacheRequest(request, input, model);
    const cached = await readCachedResult(key);
    if (cached) {
      return json(cached, 200, { "X-Live-Extract-Cache": "HIT" });
    }

    // pageURLをfetchしない。iPhoneが送った現在DOMのSnapshotだけをAIへ渡す。
    let normalized: unknown = null;
    let lastError: unknown;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const result = await env.AI.run(
          model,
          v1AIInput(model, input, attempt),
          env.AI_GATEWAY_ID ? { gateway: { id: env.AI_GATEWAY_ID } } : undefined,
        );
        const envelope = unwrapAIResult(result);
        normalized = envelope.finishReason === "length" ? null : normalizeAIResult(result);
        if (!normalized) throw new Error("invalid_ai_response");
        break;
      } catch (error) {
        lastError = error;
        const message = errorText(error);
        const dailyQuota = message.includes("3036") || message.includes("daily free allocation");
        const retryable = message.includes("invalid_ai_response") || message.includes("3040") ||
          message.includes("capacity") || message.includes("timeout") || message.includes("timed out") ||
          message.includes("json") || message.includes("rate limit") || message.includes("429");
        if (attempt === 1 || dailyQuota || !retryable) throw error;
        await new Promise((resolve) => setTimeout(resolve, 150));
      }
    }
    if (!normalized) throw lastError ?? new Error("invalid_ai_response");
    await storeCachedResult(key, normalized);
    return json(normalized, 200, { "X-Live-Extract-Cache": "MISS" });
  } catch (error) {
    const message = errorText(error);
    if (message.includes("3036") || message.includes("daily free allocation")) {
      return json(
        { code: "ai_quota_exhausted", message: "AI is temporarily unavailable", retryAt: nextUTCDate() },
        503,
      );
    }
    if (message.includes("invalid_ai_response")) {
      return json({ code: "invalid_ai_response", message: "Invalid AI response" }, 502);
    }
    return json({ code: "ai_unavailable", message: "AI is temporarily unavailable" }, 503);
  }
}
