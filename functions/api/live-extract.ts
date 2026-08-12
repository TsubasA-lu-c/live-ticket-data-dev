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

function json(body: unknown, status = 200): Response {
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
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
    return json(normalized);
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
