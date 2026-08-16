export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "assistant_call"
  | "task_result"
  | "task_cancelled"
  | "error"
  | string;

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
}

export interface CancelTaskResponse {
  status: "cancelled" | "cancelling" | string;
  thread_id: string;
  message?: string;
}

export interface UploadResponse {
  status: "uploaded" | string;
  files: string[];
}

export interface OutputFile {
  name: string;
  type: "file" | string;
  /** 会话目录内的相对路径，下载时与 thread_id 一起使用 */
  path: string;
  /** 文件所属会话，下载接口据此在服务端定位会话目录 */
  thread_id: string;
  size: number;
  mtime: number;
}

export interface FileListResponse {
  files?: OutputFile[];
  error?: string;
}

export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}
