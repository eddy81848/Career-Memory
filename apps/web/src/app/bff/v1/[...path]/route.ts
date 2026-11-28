import { handleBffProxy } from "@/server/bff/handlers";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

type ProxyRouteContext = {
  params: Promise<{ path: string[] }>;
};

async function forward(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  const { path } = await context.params;
  return handleBffProxy(request, path);
}

export async function GET(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  return forward(request, context);
}

export async function POST(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  return forward(request, context);
}

export async function PATCH(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  return forward(request, context);
}

export async function PUT(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  return forward(request, context);
}

export async function DELETE(
  request: Request,
  context: ProxyRouteContext
): Promise<Response> {
  return forward(request, context);
}
