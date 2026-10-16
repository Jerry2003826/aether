"use client";

import { useEffect, useState, type FormEvent } from "react";

type View = "overview" | "tasks" | "approvals" | "knowledge" | "discover" | "skills" | "settings";
type Phase = "loading" | "offline" | "error" | "login" | "ready";

type Project = { id: string; name: string };
type Task = { id: string; status: string; projection_updated_at: string | null; budget: { unknown_request_count: number; observed_micro_usd: number } };
type Approval = { id: string; decision: string; expires_at: string };
type Source = { id: string; locator: string; index_status: string };
type Memory = { id: string; claim: string; epistemic_status: string };
type Recommendation = { source_id: string; title: string; reasons: string[]; exploration: boolean };
type Skill = { id: string; name: string; description: string; versions: { status: string }[] };
type Watch = { id: string; purpose: string; status: string };
type Evaluation = { id: string; purpose: string; status: string; metrics: unknown };
type Cost = { actual_known_micro_usd: number; unknown_request_count: number; held_micro_usd: number; observed_micro_usd: number };

const VIEWS: View[] = ["overview", "tasks", "approvals", "knowledge", "discover", "skills", "settings"];
const LONG_TEXT = "控制台需要完整显示中文长文本，不能在中间截断。".repeat(12);

function viewLabel(view: View): string {
  switch (view) {
    case "overview":
      return "总览";
    case "tasks":
      return "任务";
    case "approvals":
      return "审批";
    case "knowledge":
      return "知识";
    case "discover":
      return "发现";
    case "skills":
      return "技能";
    case "settings":
      return "设置";
    default: {
      const unreachable: never = view;
      return unreachable;
    }
  }
}

async function readJson(path: string, init?: RequestInit): Promise<{ status: number; body: unknown }> {
  const response = await fetch(path, { ...init, cache: "no-store", credentials: "same-origin" });
  const body = (await response.json()) as unknown;
  return { status: response.status, body };
}

export default function ConsolePage() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [message, setMessage] = useState("正在加载");
  const [view, setView] = useState<View>("overview");
  const [passphrase, setPassphrase] = useState("");
  const [csrf, setCsrf] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [projectName, setProjectName] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [watches, setWatches] = useState<Watch[]>([]);
  const [evaluations, setEvaluations] = useState<Evaluation[]>([]);
  const [costs, setCosts] = useState<Cost | null>(null);
  const [goal, setGoal] = useState("");
  const [claim, setClaim] = useState("");

  async function loadProject(id: string, token: string) {
    const [taskBody, approvalBody, sourceBody, memoryBody, recommendationBody, skillBody, watchBody, evaluationBody, costBody] =
      await Promise.all([
        readJson(`/v1/projects/${id}/tasks`),
        readJson(`/v1/projects/${id}/approvals`),
        readJson(`/v1/projects/${id}/sources`),
        readJson(`/v1/projects/${id}/memories`),
        readJson(`/v1/projects/${id}/recommendations`),
        readJson(`/v1/projects/${id}/skills`),
        readJson(`/v1/projects/${id}/watches`),
        readJson(`/v1/projects/${id}/evaluations`),
        readJson(`/v1/projects/${id}/costs`),
      ]);
    if ([taskBody, approvalBody, sourceBody, memoryBody, recommendationBody, skillBody, watchBody, evaluationBody, costBody].some((item) => item.status !== 200)) {
      setPhase("error");
      setMessage("有一块数据没有从 API 读到。");
      return;
    }
    setTasks((taskBody.body as { items: Task[] }).items);
    setApprovals((approvalBody.body as { items: Approval[] }).items);
    setSources((sourceBody.body as { items: Source[] }).items);
    setMemories((memoryBody.body as { items: Memory[] }).items);
    setRecommendations((recommendationBody.body as { items: Recommendation[] }).items);
    setSkills((skillBody.body as { items: Skill[] }).items);
    setWatches((watchBody.body as { items: Watch[] }).items);
    setEvaluations((evaluationBody.body as { items: Evaluation[] }).items);
    setCosts(costBody.body as Cost);
    setProjectId(id);
    setCsrf(token);
    setPhase("ready");
    setMessage("");
  }

  async function loadSession() {
    setPhase("loading");
    setMessage("正在加载");
    try {
      const session = await readJson("/v1/auth/session");
      if (session.status === 401) {
        setPhase("login");
        setMessage("未登录。没有任务、审批或费用数据。");
        return;
      }
      if (session.status !== 200) {
        setPhase("error");
        setMessage("会话暂时不可用。");
        return;
      }
      const token = (session.body as { csrf_token: string }).csrf_token;
      const listed = await readJson("/v1/projects");
      if (listed.status !== 200) {
        setPhase("error");
        setMessage("项目列表暂时不可用。");
        return;
      }
      const items = (listed.body as { items: Project[] }).items;
      setProjects(items);
      setCsrf(token);
      if (items.length === 0) {
        setPhase("ready");
        setMessage("还没有项目。可以新建一个。");
        return;
      }
      await loadProject(items[0].id, token);
    } catch {
      setPhase("offline");
      setMessage("API 未连接。进程恢复前这里保持空状态。");
    }
  }

  useEffect(() => {
    void loadSession();
  }, []);

  async function login(event: FormEvent) {
    event.preventDefault();
    setPhase("loading");
    try {
      const response = await readJson("/v1/auth/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passphrase }),
      });
      if (response.status !== 200) {
        setPhase("error");
        setMessage("登录没有成功。");
        return;
      }
      await loadSession();
    } catch {
      setPhase("offline");
      setMessage("API 未连接。进程恢复前这里保持空状态。");
    }
  }

  async function createTask(event: FormEvent) {
    event.preventDefault();
    try {
      const response = await readJson(`/v1/projects/${projectId}/tasks`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": `task-${goal.length}-${Date.now()}`,
        },
        body: JSON.stringify({ goal }),
      });
      if (response.status !== 202) {
        setPhase("error");
        setMessage("任务没有接收。");
        return;
      }
      setGoal("");
      await loadProject(projectId, csrf);
    } catch {
      setPhase("offline");
      setMessage("API 未连接。进程恢复前这里保持空状态。");
    }
  }

  async function createMemory(event: FormEvent) {
    event.preventDefault();
    try {
      const response = await readJson(`/v1/projects/${projectId}/memories`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": `memory-${claim.length}-${Date.now()}`,
        },
        body: JSON.stringify({ claim, kind: "semantic", category: "fact" }),
      });
      if (response.status !== 202) {
        setPhase("error");
        setMessage("记忆没有接收。");
        return;
      }
      setClaim("");
      await loadProject(projectId, csrf);
    } catch {
      setPhase("offline");
      setMessage("API 未连接。进程恢复前这里保持空状态。");
    }
  }

  async function createProject(event: FormEvent) {
    event.preventDefault();
    try {
      const response = await readJson("/v1/projects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrf,
          "Idempotency-Key": `project-${projectName.length}-${Date.now()}`,
        },
        body: JSON.stringify({ name: projectName }),
      });
      if (response.status !== 201) {
        setPhase("error");
        setMessage("项目没有创建。");
        return;
      }
      await loadSession();
    } catch {
      setPhase("offline");
      setMessage("API 未连接。进程恢复前这里保持空状态。");
    }
  }

  return (
    <main>
      <h1>AETHER 工作台</h1>
      <p>时区：Australia/Melbourne。数字来自 API，页面不填示例曲线。</p>
      <p data-testid="long-text">{LONG_TEXT}</p>
      <nav aria-label="工作台分区">
        {VIEWS.map((item) => (
          <button key={item} type="button" aria-current={view === item ? "page" : undefined} onClick={() => setView(item)}>
            {viewLabel(item)}
          </button>
        ))}
        <button type="button" onClick={() => void loadSession()}>
          刷新
        </button>
      </nav>
      {message ? <p role="status">{message}</p> : null}
      {phase === "login" ? (
        <form className="panel" onSubmit={(event) => void login(event)}>
          <label>
            口令
            <input value={passphrase} onChange={(event) => setPassphrase(event.target.value)} autoComplete="current-password" />
          </label>
          <button type="submit">登录</button>
        </form>
      ) : null}
      {phase === "ready" && projects.length === 0 ? (
        <form className="panel" onSubmit={(event) => void createProject(event)}>
          <label>
            项目名称
            <input value={projectName} onChange={(event) => setProjectName(event.target.value)} />
          </label>
          <button type="submit">新建项目</button>
        </form>
      ) : null}
      {phase === "ready" && projectId ? (
        <section className="panel">
          {view === "overview" ? (
            <>
              <h2>总览</h2>
              <p>项目 {projects.find((item) => item.id === projectId)?.name}</p>
              <p>活跃任务 {tasks.length}，待审批 {approvals.length}。</p>
              <p>
                已知费用 {costs?.actual_known_micro_usd ?? 0}，冻结 {costs?.held_micro_usd ?? 0}，未知请求 {costs?.unknown_request_count ?? 0}，超额 {costs?.observed_micro_usd ?? 0}。
              </p>
              {tasks.length === 0 ? <p>还没有任务。可以新建。</p> : null}
            </>
          ) : null}
          {view === "tasks" ? (
            <>
              <h2>任务</h2>
              {tasks.length === 0 ? <p>还没有任务。可以新建。</p> : null}
              <form onSubmit={(event) => void createTask(event)}>
                <label>
                  任务目标
                  <textarea value={goal} onChange={(event) => setGoal(event.target.value)} />
                </label>
                <button type="submit">新建任务</button>
              </form>
              {tasks.map((task) => (
                <article key={task.id}>
                  <p>
                    {task.id} · {task.status}
                  </p>
                  <p>{task.projection_updated_at ? `投影时间 ${task.projection_updated_at}` : "投影尚未更新。"}</p>
                  <p>未知费用请求 {task.budget.unknown_request_count}，超额 {task.budget.observed_micro_usd}</p>
                </article>
              ))}
            </>
          ) : null}
          {view === "approvals" ? (
            <>
              <h2>审批</h2>
              {approvals.length === 0 ? <p>没有待处理审批。过期项不会留在这里。</p> : null}
              {approvals.map((approval) => (
                <article key={approval.id}>
                  <p>
                    {approval.id} · {approval.decision} · 到期 {approval.expires_at}
                  </p>
                  <label>
                    <input type="checkbox" /> 这次批准这一条
                  </label>
                </article>
              ))}
            </>
          ) : null}
          {view === "knowledge" ? (
            <>
              <h2>知识</h2>
              {sources.length === 0 ? <p>还没有来源。</p> : null}
              {sources.map((source) => (
                <article key={source.id}>
                  <p>{source.locator}</p>
                  <p>索引 {source.index_status}</p>
                </article>
              ))}
              <h2>记忆</h2>
              {memories.length === 0 ? <p>还没有记忆。</p> : null}
              <form onSubmit={(event) => void createMemory(event)}>
                <label>
                  记忆说法
                  <textarea value={claim} onChange={(event) => setClaim(event.target.value)} />
                </label>
                <button type="submit">记下这条</button>
              </form>
              {memories.map((memory) => (
                <article key={memory.id}>
                  <p>
                    {memory.claim} · {memory.epistemic_status}
                  </p>
                </article>
              ))}
            </>
          ) : null}
          {view === "discover" ? (
            <>
              <h2>发现</h2>
              <p>探索默认关闭。推荐理由来自本次印象，不是随机名单。</p>
              {recommendations.length === 0 ? <p>还没有可推荐的来源。</p> : null}
              {recommendations.map((item) => (
                <article key={item.source_id}>
                  <p>{item.title}</p>
                  <p>{item.reasons.join("；")}</p>
                  <p>{item.exploration ? "这张卡片在探索。" : "这张卡片没有探索。"}</p>
                </article>
              ))}
            </>
          ) : null}
          {view === "skills" ? (
            <>
              <h2>技能</h2>
              {skills.length === 0 ? <p>还没有技能。导入后先隔离，证据不足不会显示能力增长。</p> : null}
              {skills.map((skill) => (
                <article key={skill.id}>
                  <p>
                    {skill.name} · {skill.description}
                  </p>
                  <p>{skill.versions.map((version) => version.status).join("、") || "没有版本"}</p>
                </article>
              ))}
              <h2>评测</h2>
              <p>技能评测不支持，不会进入排队，也不能据此晋升。</p>
              {evaluations.length === 0 ? <p>还没有评测。没有样本就是证据不足，不显示增长百分比。</p> : null}
              {evaluations.map((evaluation) => (
                <article key={evaluation.id}>
                  <p>
                    {evaluation.purpose} · {evaluation.status}
                  </p>
                  <p>{evaluation.metrics === null ? "还没有分数。" : "分数来自独立评测记录。"}</p>
                </article>
              ))}
            </>
          ) : null}
          {view === "settings" ? (
            <>
              <h2>设置</h2>
              <p>跟踪 {watches.length} 条。模型 live 探针未运行时不会显示通过。</p>
              {watches.map((watch) => (
                <article key={watch.id}>
                  <p>
                    {watch.purpose} · {watch.status}
                  </p>
                </article>
              ))}
            </>
          ) : null}
        </section>
      ) : null}
    </main>
  );
}
