interface Env {
  AI: {
    run(model: string, input: Record<string, unknown>): Promise<unknown>;
  };
  AI_MODEL_PRIMARY?: string;
  AI_MODEL_DEBUG_A?: string;
  AI_MODEL_DEBUG_B?: string;
  AI_MODEL_DEBUG_C?: string;
  AI_DEBUG_MODELS_ENABLED?: string;
}
interface LiveExtractRequest {
  artistName: string;
  pageURL: string;
  pageTitle: string;
  snapshotText: string;
  debugModelAlias?: string | null;
}

const MAX_SNAPSHOT_LENGTH = 60_000;
const MAX_PERFORMANCES = 200;
const CACHE_TTL_SECONDS = 24 * 60 * 60;
const CACHE_SCHEMA_VERSION = "live-extract-v5";

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
      artistName: normalizedCacheText(input.artistName),
      pageURL: canonicalPageURL(input.pageURL),
      pageTitle: normalizedCacheText(input.pageTitle),
      snapshotText: normalizedCacheText(input.snapshotText),
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
- regionTextは都道府県・地域名だけを入れる
- venueTextは会場名だけを入れ、都道府県、(問)以降の問い合わせ先、電話番号、メールアドレス、URLを含めない
- OPEN/START、地域、会場、問い合わせ先が連続していても、それぞれを別フィールドへ分離する
- [SCHEDULE ROW]は公演一覧の1行を示す。同じSCHEDULE内の全行を漏れなく抽出し、途中の日程や後半の日程を省略しない
- [SCHEDULE ROW]を公演情報の正本として扱う。NEWS、受付、当日券などに再掲された同一日付を理由にSCHEDULE ROWを省略・置換しない
- NEWSや本文に中止・延期の記載があっても、SCHEDULEに残っている公演行は推測で除外せず抽出する。保存するかは利用者が確認する

アーティスト名: ${input.artistName}
ページURL（識別用。取得してはいけない）: ${input.pageURL}
ページタイトル: ${input.pageTitle}

表示済みページ本文:
${input.snapshotText}`;
}

function normalizeAIResult(value: unknown): unknown {
  if (value && typeof value === "object" && "response" in value) {
    value = (value as { response: unknown }).response;
  }
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }
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

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return json({ code: "invalid_request", message: "Invalid JSON" }, 400);
  }
  const input = parseRequest(body);
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

  try {
    const key = await cacheRequest(request, input, model);
    const cached = await readCachedResult(key);
    if (cached) {
      return json(cached, 200, { "X-Live-Extract-Cache": "HIT" });
    }

    // pageURLをfetchしない。iPhoneが送った現在DOMのSnapshotだけをAIへ渡す。
    const result = await env.AI.run(model, {
      messages: [
        {
          role: "system",
          content:
            "入力本文の文字列だけを根拠に抽出し、指定JSON Schema以外を返さないでください。",
        },
        { role: "user", content: prompt(input) },
      ],
      response_format: {
        type: "json_schema",
        json_schema: responseSchema,
      },
      max_tokens: 8_000,
      temperature: 0,
    });
    const normalized = normalizeAIResult(result);
    if (!normalized) {
      return json({ code: "invalid_ai_response", message: "Invalid AI response" }, 502);
    }
    await storeCachedResult(key, normalized);
    return json(normalized, 200, { "X-Live-Extract-Cache": "MISS" });
  } catch (error) {
    const message = error instanceof Error ? error.message.toLowerCase() : "";
    const quota =
      message.includes("quota") ||
      message.includes("rate limit") ||
      message.includes("neurons") ||
      message.includes("429");
    if (quota) {
      return json(
        { code: "ai_quota_exhausted", message: "AI is temporarily unavailable" },
        503,
      );
    }
    return json({ code: "ai_unavailable", message: "AI is temporarily unavailable" }, 503);
  }
}
