import { handleAuthLogin } from "@/server/bff/handlers";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  return handleAuthLogin(request);
}
