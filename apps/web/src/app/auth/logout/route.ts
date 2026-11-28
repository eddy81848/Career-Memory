import { handleAuthLogout } from "@/server/bff/handlers";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return handleAuthLogout(request);
}
