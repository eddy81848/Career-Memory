import type { Metadata } from "next";
import React from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "Career Memory — 연결 및 일회성 최초 가져오기 (로컬 UI 프로토타입)",
  description:
    "Career Memory 초기 GitHub 공개 저장소 스냅샷 가져오기 여정의 로컬 UI Fixture 프로토타입",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
