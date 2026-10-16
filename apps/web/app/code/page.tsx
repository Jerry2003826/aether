"use client";

import { useEffect, useState, type FormEvent } from "react";

type Phase = "idle" | "accepted" | "error" | "offline";
type TerminalState = "FINISHED" | "FAILED" | "CANCELLED";

function terminalMessage(state: TerminalState): string {
  switch (state) {
    case "FINISHED":
      return "作业已完成。";
    case "FAILED":
      return "作业失败，可以用同一作业键重试。";
    case "CANCELLED":
      return "作业已取消。外部效果仍是未知。";
    default: {
      const unreachable: never = state;
      return unreachable;
    }
  }
}

function isTerminalState(value: unknown): value is TerminalState {
  return value === "FINISHED" || value === "FAILED" || value === "CANCELLED";
}

function codingTerminalState(body: string, jobId: string): TerminalState | null {
  for (const line of body.split("\n")) {
    if (!line.startsWith("data: ")) {
      continue;
    }
    try {
      const parsed: unknown = JSON.parse(line.slice("data: ".length));
      if (typeof parsed !== "object" || parsed === null) {
        continue;
      }
      const event = parsed as { event_type?: unknown; payload?: unknown };
      if (event.event_type !== "coding_job.terminal" || typeof event.payload !== "object" || event.payload === null) {
        continue;
      }
      const payload = event.payload as { coding_job_id?: unknown; state?: unknown };
      if (payload.coding_job_id === jobId && isTerminalState(payload.state)) {
        return payload.state;
      }
    } catch {
      continue;
    }
  }
  return null;
}

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
  const [jobId, setJobId] = useState("");

  useEffect(() => {
    if (phase !== "accepted" || jobId === "" || projectId === "" || taskId === "") {
      return;
    }
    let stopped = false;
    const pull = () => {
      void fetch(`/v1/projects/${projectId}/tasks/${taskId}/events`, {
        cache: "no-store",
        credentials: "same-origin",
      })
        .then(async (response) => {
          if (!response.ok || stopped) {
            return;
          }
          const state = codingTerminalState(await response.text(), jobId);
          if (state !== null && !stopped) {
            setMessage(terminalMessage(state));
          }
        })
        .catch(() => undefined);
    };
    pull();
    const timer = window.setInterval(pull, 2000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [phase, jobId, projectId, taskId]);

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
      const accepted = response.body as { resource_id?: unknown };
      setJobId(typeof accepted.resource_id === "string" ? accepted.resource_id : "");
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
      <p>提交只表示已受理。沙箱断网、只读、限资源。终态是 FINISHED 或 FAILED，失败后可以用同一作业键重试。</p>
      <p>取消要等容器确认消失才是 CANCELLED，外部效果仍是未知。还持有租约的作业不会被回收。</p>
      <p>终态写在这条任务的事件流里。页面保留作业编号，并在事件到达后更新状态。</p>
      {jobId ? <p>作业编号 {jobId}</p> : null}
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
        <button type="submit">提交编码作业</button>
      </form>
      {message ? <p role="status">{message}</p> : null}
    </main>
  );
}
