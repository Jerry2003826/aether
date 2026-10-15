export type ProjectList = {
  items?: { id: string; name: string }[];
};

export function describeProjects(status: number, body: ProjectList | null): string {
  if (status === 401) {
    return "未登录。没有任务、审批或费用数据。";
  }
  if (status !== 200 || body === null) {
    return "项目列表暂时不可用。";
  }
  const items = body.items ?? [];
  if (items.length === 0) {
    return "还没有项目。";
  }
  return items.map((item) => item.name).join("、");
}
