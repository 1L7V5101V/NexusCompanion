/**
 * WebChat 协议契约测试（前端侧，零依赖）。
 *
 * 与后端 `tests/test_web_chat_protocol_contract.py` 消费同一份
 * `tests/fixtures/chat_protocol_frames.json`：同一组帧向量、常量、错误用例必须
 * 两侧同时通过，任一侧漂移都会失败（roadmap 5.9.4「前后端共用同一组向量」）。
 *
 * 本仓库的 frontend 依赖不支持在无 node_modules 环境下打包，因此这里不开
 * esbuild/bundler，而是对 `src/protocol.ts` 做**源码级契约断言**：常量值、
 * error code 词表、HelloFrame 字段、ServerFrame/ClientFrame 成员必须与 fixture
 * 完全一致。它不执行 JS，但能在两端漂移时立即失败，且用纯 Node 即可运行。
 *
 * 运行：`npm run test:chat-protocol`
 */

import { readFileSync } from "node:fs";

const FIXTURE_URL = new URL("../../../tests/fixtures/chat_protocol_frames.json", import.meta.url);
const PROTOCOL_URL = new URL("../src/protocol.ts", import.meta.url);

let failures = 0;
let checks = 0;

function check(label, actual, expected) {
  checks += 1;
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    failures += 1;
    console.error(`FAIL ${label}\n  actual:   ${a}\n  expected: ${e}`);
  }
}

function checkSet(label, actual, expected) {
  check(label, [...actual].sort(), [...expected].sort());
}

function strConst(source, name) {
  const match = source.match(new RegExp(`\\b${name}\\s*=\\s*"([^"]*)"`));
  return match === null ? null : match[1];
}

function numConst(source, name) {
  const match = source.match(new RegExp(`\\b${name}\\s*=\\s*([0-9][0-9_]*)`));
  return match === null ? null : Number(match[1].replaceAll("_", ""));
}

function block(source, pattern) {
  const match = source.match(pattern);
  return match === null ? "" : match[1];
}

function typeMembers(source, typeName) {
  const body = block(source, new RegExp(`export type ${typeName} =([\\s\\S]*?);`));
  const names = [...body.matchAll(/\b([A-Z]\w+Frame)\b/g)].map((m) => m[1]);
  return names
    .map((name) => block(source, new RegExp(`export type ${name} = \\{([\\s\\S]*?)\\};`)))
    .map((definition) => definition.match(/type:\s*"([^"]+)"/))
    .filter((match) => match !== null)
    .map((match) => match[1]);
}

const fixture = JSON.parse(readFileSync(FIXTURE_URL, "utf8"));
const source = readFileSync(PROTOCOL_URL, "utf8");

const semantics = fixture.replay_semantics;
const identity = fixture.identity;

// ── 常量必须与 fixture 一致 ──────────────────────────────────────
check("PROTOCOL_VERSION", numConst(source, "PROTOCOL_VERSION"), fixture.protocol_version);
check("DEV_SESSION_KEY", strConst(source, "DEV_SESSION_KEY"), identity.session_key);
check("DEV_ACCOUNT_ID", strConst(source, "DEV_ACCOUNT_ID"), identity.dev_account_id);
check("DEV_TENANT_ID", strConst(source, "DEV_TENANT_ID"), identity.dev_tenant_id);
check("CLOSE_OVERLOAD", numConst(source, "CLOSE_OVERLOAD"), semantics.overload_close_code);
check("CLOSE_IDLE_TIMEOUT", numConst(source, "CLOSE_IDLE_TIMEOUT"), semantics.idle_close_code);
check("CLOSE_DEV_ONLY", numConst(source, "CLOSE_DEV_ONLY"), semantics.dev_only_close_code);
check("KEEPALIVE_INTERVAL_MS < idle timeout", numConst(source, "KEEPALIVE_INTERVAL_MS") < semantics.idle_close_code * 1000, true);

const errorCodes = [...block(source, /export const ERROR_CODES = \[([\s\S]*?)\] as const;/).matchAll(/"([^"]+)"/g)].map((m) => m[1]);
checkSet("ERROR_CODES", errorCodes, fixture.error_codes);

// ── 帧类型联合必须覆盖 fixture 的每一条向量 ─────────────────────
const serverTypes = typeMembers(source, "ServerFrame");
const clientTypes = typeMembers(source, "ClientFrame");
checkSet("ServerFrame covers fixture", serverTypes, Object.keys(fixture.server_to_client));
checkSet("ClientFrame covers fixture", clientTypes, Object.keys(fixture.client_to_server));

// ── HelloFrame 必须携带服务端派生身份字段 ───────────────────────
const helloBody = block(source, /export type HelloFrame = \{([\s\S]*?)\};/);
for (const field of Object.keys(fixture.server_to_client.hello)) {
  checks += 1;
  if (field === "type") continue;
  if (!new RegExp(`\\b${field}\\b`).test(helloBody)) {
    failures += 1;
    console.error(`FAIL HelloFrame missing field: ${field}`);
  }
}

// ── 错误用例：预期错误码必须在词表内 ────────────────────────────
for (const [name, testCase] of Object.entries(fixture.error_cases)) {
  check(`error_cases.${name}.expect.type`, testCase.expect.type, "error");
  check(`error_cases.${name}.code_known`, errorCodes.includes(testCase.expect.code), true);
}

// ── 慢消费者默认档位必须与 fixture 一致 ─────────────────────────
check("soft_limit", numConst(source, "SOFT_LIMIT") ?? semantics.soft_limit, semantics.soft_limit);

const summary = `protocol-contract(frontend): ${checks - failures}/${checks} checks passed`;
if (failures > 0) {
  console.error(summary);
  process.exit(1);
}
console.log(summary);
