const apiBase = process.env.NEXT_PUBLIC_AETHER_API_BASE ?? "http://127.0.0.1:8000";

export default function CodeTaskPage() {
  return (
    <main>
      <h1>代码任务</h1>
      <p>候选、验收和失败状态只来自 API（{apiBase}）。这里没有示例补丁，也没有写死的通过结果。</p>
      <p>验收绑定套件、合同和候选的哈希。候选一变，之前的通过就失效。</p>
      <ul>
        <li>worker 中断：没有写出通过结果。</li>
        <li>runner 停止：容器不再运行，不能当成通过。</li>
        <li>取消：状态是已取消，外部效果仍是未知。</li>
      </ul>
    </main>
  );
}
