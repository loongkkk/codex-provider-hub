"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const sourcePath = path.join(__dirname, "..", "proxy_static", "app.js");
const source = fs.readFileSync(sourcePath, "utf8");
const start = source.indexOf("function requestRecordKey");
const end = source.indexOf("function populateRequestProviders");
assert.notEqual(start, -1);
assert.notEqual(end, -1);

const context = vm.createContext({});
vm.runInContext(
  `${source.slice(start, end)}\nthis.api = {
    formatRequestDuration,
    requestResultLabel,
    requestActualProviderLabel,
  };`,
  context,
  { filename: sourcePath },
);

test("formats short and long request durations", () => {
  assert.equal(context.api.formatRequestDuration(420), "420ms");
  assert.equal(context.api.formatRequestDuration(4200), "4.2s");
  assert.equal(context.api.formatRequestDuration(125000), "2:05");
});

test("labels running, successful, and failed request results", () => {
  assert.equal(
    context.api.requestResultLabel({ state: "running", outcome: "receiving" }),
    "接收中",
  );
  assert.equal(
    context.api.requestResultLabel({ state: "running", outcome: "retrying", retry_count: 2 }),
    "第 3 次尝试",
  );
  assert.equal(
    context.api.requestResultLabel({ succeeded: true, status_code: 200 }),
    "HTTP 200",
  );
  assert.equal(
    context.api.requestResultLabel({ succeeded: false, error_summary: "响应流中断" }),
    "响应流中断",
  );
});
test("keeps the actual provider visible in each request row", () => {
  assert.equal(
    context.api.requestActualProviderLabel({ state: "running", provider_name: "公司" }),
    "正在使用 · 公司",
  );
  assert.equal(
    context.api.requestActualProviderLabel({ state: "failed", provider_name: "zzzcoding" }),
    "本次使用 · zzzcoding",
  );
  assert.doesNotMatch(source, /request-route-select|claimRequestRouteControl/);
});
test("opens the request page without provider or status filters", () => {
  const openStart = source.indexOf("function openAllRequests");
  const openEnd = source.indexOf("function switchView");
  assert.notEqual(openStart, -1);
  assert.notEqual(openEnd, -1);
  const openSource = source.slice(openStart, openEnd);
  assert.match(openSource, /requestProvider\.value = ""/);
  assert.match(openSource, /requestStatus\.value = "all"/);
  assert.doesNotMatch(source, /悬停查看会话，点击进入请求页/);
});
test("deduplicates active session names for the provider hover list", () => {
  const activeStart = source.indexOf("function activeSessionNames");
  const activeEnd = source.indexOf("function renderActiveSessionsPopover");
  assert.notEqual(activeStart, -1);
  assert.notEqual(activeEnd, -1);
  const activeContext = vm.createContext({});
  vm.runInContext(
    `${source.slice(activeStart, activeEnd)}\nthis.activeSessionNames = activeSessionNames;`,
    activeContext,
    { filename: sourcePath },
  );

  assert.deepEqual(
    Array.from(activeContext.activeSessionNames({
      active_sessions: [
        { name: "设备打卡" },
        { name: "设备打卡" },
        { name: "Codex服务可用检测" },
      ],
    })),
    ["设备打卡", "Codex服务可用检测"],
  );
});
