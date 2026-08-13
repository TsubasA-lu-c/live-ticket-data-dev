import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import {
  boundedMapOrdered, normalizeAIResult, onRequest, parseV2Request, validateV2ChunkResult,
} from "./live-extract.ts";

globalThis.caches = {
  default: {
    async match() { return undefined; },
    async put() {},
  },
};

const block = {
  blockId: "event-1",
  pageURL: "https://example.com/live",
  sectionPath: ["TOUR 2026"],
  type: "event",
  text: "TOUR 2026 2026年8月13日 東京都 東京ドーム OPEN 17:00 START 18:00",
  expectedEvent: true,
};

function request(overrides = {}) {
  return {
    artistName: "Example",
    pageURL: "https://example.com/live",
    pageTitle: "Live",
    snapshotText: block.text,
    debugModelAlias: null,
    contractVersion: 2,
    extractorVersion: "fixture-v2",
    requestId: "request-12345678",
    document: {
      canonicalURL: "https://example.com/live",
      title: "Live",
      locale: "ja-JP",
      contentHash: "a".repeat(64),
    },
    capture: {
      truncated: false,
      pagesAttempted: ["https://example.com/live"],
      pagesSucceeded: ["https://example.com/live"],
      pagesFailed: [],
    },
    chunks: [{ chunkId: "chunk-1", blocks: [block] }],
    forceRefresh: false,
    ...overrides,
  };
}

function requestWithValidContentHash(overrides = {}) {
  const input = request({ forceRefresh: true, ...overrides });
  const hashSource = input.chunks.flatMap((chunk) => chunk.blocks).map((item) => [
    item.blockId, item.pageURL, item.sectionPath.join(" > "), item.type, item.text,
  ].join("\u001f")).join("\u001e");
  return {
    ...input,
    document: {
      ...input.document,
      contentHash: createHash("sha256").update(hashSource).digest("hex"),
    },
  };
}

function validModelResult(performanceOverrides = {}) {
  return {
    performances: [{
      sourceBlockId: "event-1",
      groupTitleText: "TOUR 2026",
      dateText: "2026年8月13日",
      regionText: "東京都",
      venueText: "東京ドーム",
      openTimeText: "17:00",
      startTimeText: "18:00",
      ...performanceOverrides,
    }],
  };
}

const validV1Result = {
  performances: [{
    groupTitleText: "TOUR 2026", dateText: "2026年8月13日", regionText: "東京都",
    venueText: "東京ドーム", openTimeText: "17:00", startTimeText: "18:00", kind: "tour",
  }],
};

test("parseV2Request accepts the valid fixture", () => {
  assert.ok(parseV2Request(request()));
});

test("v1 normalization unwraps OpenAI choices parsed, content, and text", () => {
  const envelopes = [
    { choices: [{ finish_reason: "stop", message: { parsed: validV1Result } }] },
    { choices: [{ finish_reason: "stop", message: { content: JSON.stringify(validV1Result) } }] },
    { choices: [{ finish_reason: "stop", text: JSON.stringify(validV1Result) }] },
    { response: { choices: [{ finish_reason: "stop", message: {
      content: [{ type: "text", text: `\`\`\`json\n${JSON.stringify(validV1Result)}\n\`\`\`` }],
    } }] } },
  ];
  for (const envelope of envelopes) {
    assert.deepEqual(normalizeAIResult(envelope), validV1Result);
  }
});

test("v1 normalization rejects choice finish_reason length", () => {
  assert.equal(normalizeAIResult({
    choices: [{ finish_reason: "length", message: { parsed: validV1Result } }],
  }), null);
});

test("boundedMapOrdered limits concurrency to two and preserves input order", async () => {
  let active = 0;
  let maximumActive = 0;
  const result = await boundedMapOrdered([0, 1, 2, 3, 4], 2, async (value) => {
    active += 1;
    maximumActive = Math.max(maximumActive, active);
    await new Promise((resolve) => setTimeout(resolve, (5 - value) * 2));
    active -= 1;
    return `result-${value}`;
  });
  assert.equal(maximumActive, 2);
  assert.deepEqual(result, ["result-0", "result-1", "result-2", "result-3", "result-4"]);
});

test("boundedMapOrdered stops claiming queued work after the first failure", async () => {
  const started = [];
  let releaseSecond;
  const secondIsRunning = new Promise((resolve) => { releaseSecond = resolve; });
  await assert.rejects(
    boundedMapOrdered([0, 1, 2, 3], 2, async (value) => {
      started.push(value);
      if (value === 0) {
        await secondIsRunning;
        throw { code: 3036, message: "daily free allocation exhausted" };
      }
      if (value === 1) {
        releaseSecond();
        await new Promise((resolve) => setTimeout(resolve, 5));
        return value;
      }
      return value;
    }),
  );
  assert.deepEqual(started, [0, 1]);
});

test("parseV2Request accepts a no-event document with no chunks", () => {
  assert.ok(parseV2Request(request({ chunks: [], document: {
    canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP",
    contentHash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  } })));
});

test("parseV2Request rejects duplicate block IDs across chunks", () => {
  assert.equal(parseV2Request(request({
    chunks: [
      { chunkId: "chunk-1", blocks: [block] },
      { chunkId: "chunk-2", blocks: [{ ...block }] },
    ],
  })), null);
});

test("parseV2Request rejects oversized blocks", () => {
  assert.equal(parseV2Request(request({
    chunks: [{ chunkId: "chunk-1", blocks: [{ ...block, text: "x".repeat(30_001) }] }],
  })), null);
});

test("validateV2ChunkResult accepts compact exact response and synthesizes date evidence", () => {
  const result = validateV2ChunkResult(validModelResult(), [block]);
  assert.equal(result?.performances.length, 1);
  assert.equal(result?.performances[0].evidenceText, "2026年8月13日");
  assert.equal(result?.groups[0].titleText, "TOUR 2026");
  assert.equal(result?.rejected.length, 0);
  assert.equal(result?.cacheable, true);
});

test("validateV2ChunkResult restores quote, width, and space variants to exact source title", () => {
  const styled = {
    ...block,
    text: "ＴＯＵＲ　２０２６『Flügel Letter』 2026年8月13日 東京都 東京ドーム OPEN 17:00 START 18:00",
  };
  const result = validateV2ChunkResult(validModelResult({
    groupTitleText: "TOUR 2026「Flügel Letter」",
  }), [styled]);
  assert.equal(result?.performances.length, 1);
  assert.equal(result?.groups[0].titleText, "ＴＯＵＲ　２０２６『Flügel Letter』");
  assert.deepEqual(result?.warnings, []);
});

test("validateV2ChunkResult completes a grounded title through its year and subtitle", () => {
  const tour = {
    ...block,
    text: "TOUR 2026/09/24 (Thu) sumika Live Tour 2026 『Flügel Letter』 【大阪】Zepp Osaka Bayside 開場18:00/開演19:00",
  };
  const result = validateV2ChunkResult(validModelResult({
    groupTitleText: "sumika Live Tour 2026",
    dateText: "2026/09/24",
    regionText: "大阪",
    venueText: "Zepp Osaka Bayside",
    openTimeText: "18:00",
    startTimeText: "19:00",
  }), [tour]);
  assert.equal(result?.performances.length, 1);
  assert.equal(result?.groups[0].titleText, "sumika Live Tour 2026 『Flügel Letter』");
});

test("validateV2ChunkResult clears an ungrounded title without dropping the performance", () => {
  const result = validateV2ChunkResult(validModelResult({ groupTitleText: "INVENTED TOUR" }), [block]);
  assert.equal(result?.performances.length, 1);
  assert.equal(result?.groups[0].titleText, "");
  assert.equal(result?.performances[0].evidenceText, "2026年8月13日");
  assert.match(result?.warnings.join("\n") ?? "", /title cleared/);
  assert.equal(result?.cacheable, false);
});

test("validateV2ChunkResult unwraps OpenAI choices message.parsed", () => {
  const result = validateV2ChunkResult({
    choices: [{ finish_reason: "stop", message: { parsed: validModelResult() } }],
    usage: { prompt_tokens: 10, completion_tokens: 20 },
  }, [block]);
  assert.equal(result?.performances.length, 1);
  assert.deepEqual(result?.usage, { prompt_tokens: 10, completion_tokens: 20 });
});

test("validateV2ChunkResult unwraps OpenAI choices message.content JSON", () => {
  const result = validateV2ChunkResult({
    choices: [{ finish_reason: "stop", message: { content: JSON.stringify(validModelResult()) } }],
  }, [block]);
  assert.equal(result?.performances.length, 1);
});

test("validateV2ChunkResult unwraps nested response choices and content parts", () => {
  const result = validateV2ChunkResult({
    response: {
      choices: [{ finish_reason: "stop", message: {
        content: [{ type: "text", text: `Here is the JSON:\n${JSON.stringify(validModelResult())}` }],
      } }],
      usage: { prompt_tokens: 11, completion_tokens: 21 },
    },
  }, [block]);
  assert.equal(result?.performances.length, 1);
  assert.deepEqual(result?.usage, { prompt_tokens: 11, completion_tokens: 21 });
});

test("validateV2ChunkResult treats choice finish_reason length as invalid", () => {
  const result = validateV2ChunkResult({
    choices: [{ finish_reason: "length", text: JSON.stringify(validModelResult()) }],
  }, [block]);
  assert.equal(result, null);
});

test("validateV2ChunkResult rejects cross-block field mixing", () => {
  const other = {
    ...block,
    blockId: "event-2",
    text: "2026年8月14日 大阪府 大阪城ホール OPEN 16:00 START 17:00",
  };
  const result = validateV2ChunkResult(validModelResult({ dateText: "2026年8月14日" }), [block, other]);
  assert.equal(result?.performances.length, 0);
  assert.match(result?.rejected[0].reason ?? "", /required field is not exact source substring/);
});

test("onRequest rejects an oversized Content-Length before parsing JSON", async () => {
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST",
      headers: { "content-type": "application/json", "content-length": "900001" },
      body: "{}",
    }),
    env: { AI: { async run() { throw new Error("must not run"); } } },
  });
  assert.equal(response.status, 413);
  assert.equal((await response.json()).code, "request_too_large");
});

test("only Workers AI 3036 is reported as daily quota exhaustion", async () => {
  let calls = 0;
  const base = {
    artistName: "Example",
    pageURL: "https://example.com/live",
    pageTitle: "Live",
    snapshotText: "2026年8月13日 東京ドーム",
  };
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(base),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run() { calls += 1; throw { code: 3036, message: "daily free allocation exhausted" }; } },
    },
  });
  const payload = await response.json();
  assert.equal(calls, 1);
  assert.equal(payload.code, "ai_quota_exhausted");
  assert.match(payload.retryAt, /T00:00:00\.000Z$/);
});

test("out-of-capacity is retried once and is not called quota exhaustion", async () => {
  let calls = 0;
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        artistName: "Example", pageURL: "https://example.com/live", pageTitle: "Live",
        snapshotText: "2026年8月13日 東京ドーム",
      }),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run() { calls += 1; throw { code: 3040, message: "capacity" }; } },
    },
  });
  assert.equal(calls, 2);
  assert.equal((await response.json()).code, "ai_unavailable");
});

test("paid-required models are rejected before AI.run", async () => {
  let calls = 0;
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
        artistName: "Example", pageURL: "https://example.com/live", pageTitle: "Live",
        snapshotText: "2026年8月13日 東京ドーム",
      }),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/moonshotai/kimi-k2.5",
      AI: { async run() { calls += 1; return {}; } },
    },
  });
  assert.equal(calls, 0);
  assert.equal((await response.json()).code, "ai_unavailable");
});

test("v1 uses GLM completion controls and Llama token controls", async () => {
  const observations = [];
  for (const model of [
    "@cf/zai-org/glm-4.7-flash",
    "@cf/meta/llama-3.1-8b-instruct-fast",
  ]) {
    const response = await onRequest({
      request: new Request("https://worker.example/api/live-extract", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({
          artistName: "Example", pageURL: "https://example.com/live", pageTitle: "Live",
          snapshotText: `2026年8月13日 東京ドーム ${model}`,
        }),
      }),
      env: {
        AI_MODEL_PRIMARY: model,
        AI: { async run(selectedModel, payload) {
          observations.push({ model: selectedModel, payload });
          return validV1Result;
        } },
      },
    });
    assert.equal(response.status, 200);
  }

  assert.equal(observations[0].model, "@cf/zai-org/glm-4.7-flash");
  assert.equal(observations[0].payload.max_completion_tokens, 8_000);
  assert.equal(observations[0].payload.max_tokens, undefined);
  assert.equal(observations[0].payload.chat_template_kwargs?.enable_thinking, false);
  assert.equal(observations[1].model, "@cf/meta/llama-3.1-8b-instruct-fast");
  assert.equal(observations[1].payload.max_tokens, 8_000);
  assert.equal(observations[1].payload.max_completion_tokens, undefined);
  assert.equal("chat_template_kwargs" in observations[1].payload, false);
});

test("v2 debug alias selects Llama only when its environment gate is enabled", async () => {
  const input = requestWithValidContentHash({ debugModelAlias: "a" });
  const selected = [];
  const payloads = [];
  for (const enabled of [true, false]) {
    const response = await onRequest({
      request: new Request("https://worker.example/api/live-extract", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
      }),
      env: {
        AI_MODEL_PRIMARY: "@cf/zai-org/glm-4.7-flash",
        AI_MODEL_DEBUG_A: "@cf/meta/llama-3.1-8b-instruct-fast",
        AI_DEBUG_MODELS_ENABLED: enabled ? "true" : "false",
        AI: { async run(model, payload) {
          selected.push(model);
          payloads.push(payload);
          return validModelResult();
        } },
      },
    });
    assert.equal(response.status, 200);
  }

  assert.deepEqual(selected, [
    "@cf/meta/llama-3.1-8b-instruct-fast",
    "@cf/zai-org/glm-4.7-flash",
  ]);
  assert.equal(payloads[0].max_tokens, 900);
  assert.equal(payloads[0].max_completion_tokens, undefined);
  assert.equal("chat_template_kwargs" in payloads[0], false);
  assert.equal(payloads[1].max_completion_tokens, 900);
  assert.equal(payloads[1].max_tokens, undefined);
  assert.equal(payloads[1].chat_template_kwargs?.enable_thinking, false);
  for (const payload of payloads) {
    const schema = payload.response_format?.json_schema;
    assert.deepEqual(Object.keys(schema.properties), ["performances"]);
    assert.deepEqual(schema.required, ["performances"]);
    const performance = schema.properties.performances.items;
    assert.deepEqual(Object.keys(performance.properties), [
      "sourceBlockId", "groupTitleText", "dateText", "regionText", "venueText",
      "openTimeText", "startTimeText",
    ]);
    assert.equal("evidenceText" in performance.properties, false);
    assert.equal("groups" in schema.properties, false);
    assert.equal("rejected" in schema.properties, false);
  }
});

test("v2 no-event documents return empty coverage without invoking AI", async () => {
  let calls = 0;
  const input = request({ chunks: [], document: {
    canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP",
    contentHash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  } });
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run() { calls += 1; return {}; } },
    },
  });
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(calls, 0);
  assert.deepEqual(payload.performances, []);
  assert.deepEqual(payload.coverage.uncoveredBlockIds, []);
});

test("v2 invokes AI once per chunk and aggregates validated coverage", async () => {
  const second = {
    ...block,
    blockId: "event-2",
    text: "LIVE 2026 2026年8月14日 大阪府 大阪城ホール OPEN 16:00 START 17:00",
  };
  const blocks = [block, second];
  const hashSource = blocks.map((item) => [
    item.blockId, item.pageURL, item.sectionPath.join(" > "), item.type, item.text,
  ].join("\u001f")).join("\u001e");
  const contentHash = createHash("sha256").update(hashSource).digest("hex");
  const input = request({
    document: { canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP", contentHash },
    chunks: [{ chunkId: "chunk-1", blocks: [block] }, { chunkId: "chunk-2", blocks: [second] }],
    forceRefresh: true,
  });
  let calls = 0;
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run(_model, payload) {
        calls += 1;
        const isSecond = payload.messages[1].content.includes('"blockId":"event-2"');
        return isSecond ? {
          performances: [{
            sourceBlockId: "event-2", groupTitleText: "LIVE 2026", dateText: "2026年8月14日",
            regionText: "大阪府", venueText: "大阪城ホール", openTimeText: "16:00",
            startTimeText: "17:00",
          }],
        } : validModelResult();
      } },
    },
  });
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(calls, 2);
  assert.equal(payload.contractVersion, 2);
  assert.equal(payload.groups.length, 2);
  assert.equal(new Set(payload.groups.map((group) => group.groupId)).size, 2);
  assert.equal(payload.performances.length, 2);
  assert.deepEqual(payload.coverage.coveredBlockIds, ["event-1", "event-2"]);
  assert.deepEqual(payload.coverage.uncoveredBlockIds, []);
  assert.equal("kind" in payload.performances[0], false);
});

test("v2 invalid structured result does not trigger a second AI call", async () => {
  const hashSource = [block.blockId, block.pageURL, block.sectionPath.join(" > "), block.type, block.text]
    .join("\u001f");
  const input = request({
    document: {
      canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP",
      contentHash: createHash("sha256").update(hashSource).digest("hex"),
    },
    forceRefresh: true,
  });
  const formats = [];
  const models = [];
  const thinking = [];
  const tokenLimits = [];
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/zai-org/glm-4.7-flash",
      AI: { async run(model, payload) {
        models.push(model);
        formats.push(payload.response_format?.type);
        thinking.push(payload.chat_template_kwargs?.enable_thinking);
        tokenLimits.push([payload.max_completion_tokens, payload.max_tokens]);
        return { response: "not valid JSON" };
      } },
    },
  });
  assert.equal(response.status, 502);
  assert.deepEqual(formats, ["json_schema"]);
  assert.deepEqual(models, ["@cf/zai-org/glm-4.7-flash"]);
  assert.deepEqual(thinking, [false]);
  assert.deepEqual(tokenLimits, [[900, undefined]]);
  assert.equal((await response.json()).code, "invalid_ai_response");
  assert.equal(response.headers.get("X-Live-Extract-Version"), "live-extract-worker-v2.6.1");
});

test("v2 invalid response exposes shape diagnostics without response content", async () => {
  const hashSource = [block.blockId, block.pageURL, block.sectionPath.join(" > "), block.type, block.text]
    .join("\u001f");
  const input = request({
    document: {
      canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP",
      contentHash: createHash("sha256").update(hashSource).digest("hex"),
    },
    forceRefresh: true,
  });
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/google/gemma-4-26b-a4b-it",
      AI: { async run() { return { response: "private response content" }; } },
    },
  });
  assert.equal(response.status, 502);
  const diagnostic = response.headers.get("X-AI-Response-Shape") ?? "";
  assert.match(diagnostic, /raw:object\(response\);response:string\(24\);unwrapped:null/);
  assert.doesNotMatch(diagnostic, /private response content/);
});

test("v2 keeps an ungrounded-title performance covered with one AI call", async () => {
  let calls = 0;
  const input = requestWithValidContentHash();
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run() {
        calls += 1;
        return validModelResult({ groupTitleText: "INVENTED TOUR" });
      } },
    },
  });
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(calls, 1);
  assert.equal(payload.performances.length, 1);
  assert.equal(payload.groups[0].titleText, "");
  assert.deepEqual(payload.coverage.coveredBlockIds, ["event-1"]);
  assert.deepEqual(payload.coverage.uncoveredBlockIds, []);
  assert.match(payload.warnings.join("\n"), /title cleared/);
  assert.equal(response.headers.get("X-Live-Extract-AI-Calls"), "1");
});

test("v2 rejects a cross-block field and leaves the event uncovered without a second AI call", async () => {
  const context = {
    ...block,
    blockId: "context-2",
    type: "context",
    expectedEvent: false,
    text: "LIVE 2026 2026年8月14日 大阪府 大阪城ホール OPEN 16:00 START 17:00",
  };
  const blocks = [block, context];
  const hashSource = blocks.map((item) => [
    item.blockId, item.pageURL, item.sectionPath.join(" > "), item.type, item.text,
  ].join("\u001f")).join("\u001e");
  const input = request({
    document: {
      canonicalURL: "https://example.com/live", title: "Live", locale: "ja-JP",
      contentHash: createHash("sha256").update(hashSource).digest("hex"),
    },
    chunks: [{ chunkId: "chunk-1", blocks }],
    forceRefresh: true,
  });
  let calls = 0;
  const response = await onRequest({
    request: new Request("https://worker.example/api/live-extract", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input),
    }),
    env: {
      AI_MODEL_PRIMARY: "@cf/meta/llama-3.1-8b-instruct",
      AI: { async run() {
        calls += 1;
        return validModelResult({ dateText: "2026年8月14日" });
      } },
    },
  });
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(calls, 1);
  assert.deepEqual(payload.performances, []);
  assert.deepEqual(payload.coverage.coveredBlockIds, []);
  assert.deepEqual(payload.coverage.uncoveredBlockIds, ["event-1"]);
  assert.equal(payload.coverage.rejected[0].blockId, "event-1");
  assert.match(payload.coverage.rejected[0].reason, /required field is not exact source substring/);
  assert.equal(response.headers.get("X-Live-Extract-AI-Calls"), "1");
});
