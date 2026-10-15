import { describeProjects, type ProjectList } from "../lib/live";

const apiBase = process.env.NEXT_PUBLIC_AETHER_API_BASE ?? "http://127.0.0.1:8000";

type Health = {
  status: string;
  dependencies: Record<string, unknown>;
};

async function loadProjects(): Promise<{ status: number; body: ProjectList | null }> {
  try {
    const response = await fetch(`${apiBase}/v1/projects`, { cache: "no-store" });
    if (!response.ok) {
      return { status: response.status, body: null };
    }
    return { status: response.status, body: (await response.json()) as ProjectList };
  } catch {
    return { status: 0, body: null };
  }
}

async function loadHealth(): Promise<{ live: Health | null; ready: Health | null; readyStatus: number | null }> {
  try {
    const liveResponse = await fetch(`${apiBase}/health/live`, { cache: "no-store" });
    const readyResponse = await fetch(`${apiBase}/health/ready`, { cache: "no-store" });
    const live = (await liveResponse.json()) as Health;
    const ready = (await readyResponse.json()) as Health;
    return { live, ready, readyStatus: readyResponse.status };
  } catch {
    return { live: null, ready: null, readyStatus: null };
  }
}

export default async function HomePage() {
  const health = await loadHealth();
  const projects = await loadProjects();
  return (
    <main>
      <h1>AETHER 控制台</h1>
      <p>
        <a href="/console">进入工作台</a>
      </p>
      <p>时区：Australia/Melbourne。任务、审批和费用只来自实时 API，没有示例数据。</p>
      <p>API：{apiBase}</p>
      {health.live === null ? (
        <p>API 未连接。进程就绪前这里保持空状态。</p>
      ) : (
        <>
          <p>存活：{health.live.status}</p>
          <p>
            就绪：{health.ready?.status ?? "未知"}（HTTP {health.readyStatus ?? "无"}）
          </p>
        </>
      )}
      <p>{describeProjects(projects.status, projects.body)}</p>
    </main>
  );
}
