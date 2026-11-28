import { NextResponse } from "next/server";
import { PUBLIC_HEALTH_CACHE_CONTROL } from "@/server/bff/config";

export const dynamic = "force-dynamic";

export function GET(): NextResponse {
  return NextResponse.json(
    {
      status: "ok",
      service: "career-memory-web",
    },
    {
      status: 200,
      headers: {
        "Cache-Control": PUBLIC_HEALTH_CACHE_CONTROL,
        "X-Content-Type-Options": "nosniff",
      },
    }
  );
}

export function HEAD(): NextResponse {
  return new NextResponse(null, {
    status: 200,
    headers: {
      "Cache-Control": PUBLIC_HEALTH_CACHE_CONTROL,
      "X-Content-Type-Options": "nosniff",
    },
  });
}
