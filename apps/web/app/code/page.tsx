"use client";

import { useState, type FormEvent } from "react";

type Phase = "idle" | "accepted" | "error" | "offline";

async function readJson(path: string, init?: RequestInit): Promise<{ status: number; body: unknown }> {
  const response = await fetch(path, { ...init, cache: "no-store", credentials: "same-origin" });
  const body = (await response.json()) as unknown;
  return { status: response.status, body };
}

export default function CodeTaskPage() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState("");
  const [projectId, setProjectId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [artifactId, setArtifactId] = useState("");
  const [jobKey, setJobKey] = useState("");
  const [objective, setObjective] = useState("");

  async function createJob(event: FormEvent) {
    event.preventDefault();
    try {
      const session = await readJson("/v1/auth/session");
      if (session.status !== 200) {
        setPhase("error");
        setMessage("还没有登录。");
        return;
      }
      const token = (session.body as { csrf_token: string }).csrf_token;
      const response = await readJson(`/v1/projects/${projectId}/coding-jobs`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": token,
          "Idempotency-Key": `code-${jobKey}-${Date.now()}`,
        },
        body: JSON.stringify({
          task_id: taskId,
          base_artifact_id: artifactId,
          job_key: jobKey,
          objective,
        }),
      });
      if (response.status !== 202) {
        setPhase("error");
        setMessage("编码作业没有接收。");
        return;
      }
      setPhase("accepted");
      setMessage("作业已排队，还没有通过结果。");
    } catch {
      setPhase("offline");
      setMessage("API 未连接。");
    }
  }

  return (
    <main>
      <h1>代码任务</h1>
      <p>候选、验收和失败状态只来自 API。这里没有示例补丁，也没有写死的通过结果。</p>
      <p>验收绑定套件、合同和候选的哈希。候选一变，之前的通过就失效。</p>
      <ul>
        <li>worker 中断：没有写出通过结果。</li>
        <li>runner 停止：容器不再运行，不能当成通过。</li>
        <li>取消：状态是已取消，外部效果仍是未知。</li>
      </ul>
      <form onSubmit={(event) => void createJob(event)}>
        <label>
          项目编号
          <input value={projectId} onChange={(event) => setProjectId(event.target.value)} />
        </label>
        <label>
          任务编号
          <input value={taskId} onChange={(event) => setTaskId(event.target.value)} />
        </label>
        <label>
          基线产物
          <input value={artifactId} onChange={(event) => setArtifactId(event.target.value)} />
        </label>
        <label>
          作业键
          <input value={jobKey} onChange={(event) => setJobKey(event.target.value)} />
        </label>
        <label>
          目标
          <textarea value={objective} onChange={(event) => setObjective(event.target.value)} />
        </label>
        <button type="submit">排队编码作业</button>
      </form>
      {message ? <p role="status">{message}</p> : null}
    </main>
  );
}
