import assert from "node:assert/strict";
import { describeProjects } from "./live.ts";

assert.equal(describeProjects(401, null), "未登录。没有任务、审批或费用数据。");
assert.equal(describeProjects(200, { items: [] }), "还没有项目。");
assert.equal(describeProjects(200, { items: [{ id: "p", name: "甲" }] }), "甲");
assert.equal(describeProjects(503, null), "项目列表暂时不可用。");
console.log("live display ok");
