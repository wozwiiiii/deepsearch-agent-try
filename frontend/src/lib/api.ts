import { API_BASE_URL, API_KEY } from "./config";
import type { CancelTaskResponse, FileListResponse, TaskResponse, UploadResponse } from "../types";

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (API_KEY && !headers.has("X-API-Key")) {
    headers.set("X-API-Key", API_KEY);
  }
  const response = await fetch(input, { ...init, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

export async function startTask(query: string, threadId: string): Promise<TaskResponse> {
  return requestJson<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return requestJson<CancelTaskResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`), {
    method: "POST"
  });
}

export async function uploadSessionFiles(
  files: File[],
  threadId: string
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return requestJson<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}

export async function listSessionFiles(threadId: string): Promise<FileListResponse> {
  const url = new URL(apiUrl("/api/files"));
  url.searchParams.set("thread_id", threadId);
  return requestJson<FileListResponse>(url);
}

export interface LinkTokenResponse {
  token: string;
  expires_in: number;
}

export async function fetchLinkToken(): Promise<LinkTokenResponse> {
  // 请求头认证（requestJson 自动注入 X-API-Key），换 60 秒短时令牌供 WS 握手用
  return requestJson<LinkTokenResponse>(apiUrl("/api/token"), {
    method: "POST"
  });
}

export async function downloadSessionFile(threadId: string, path: string): Promise<void> {
  const url = new URL(apiUrl("/api/download"));
  url.searchParams.set("thread_id", threadId);
  url.searchParams.set("path", path);

  // fetch 可携带请求头：密钥不出现在 URL，也不进访问日志/浏览器历史；
  // 拿到 blob 后再用临时 <a> 触发浏览器另存为
  const headers = new Headers();
  if (API_KEY) {
    headers.set("X-API-Key", API_KEY);
  }
  const response = await fetch(url, { headers });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload?.detail) {
        message = String(payload.detail);
      }
    } catch {
      // 非 JSON 响应体（如网关错误页），保留默认消息
    }
    throw new Error(message);
  }

  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = path.split("/").pop() || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}
