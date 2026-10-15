import type { ReactNode } from "react";

export const metadata = {
  title: "AETHER",
  description: "本地工作台",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
