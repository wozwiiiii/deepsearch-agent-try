import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelTask,
  fetchLinkToken,
  listSessionFiles,
  startTask,
  uploadSessionFiles
} from "../lib/api";
import { API_KEY, WS_BASE_URL } from "../lib/config";
import { createThreadId, getStoredThreadId, storeThreadId } from "../lib/thread";
import type {
  ConnectionState,
  MonitorMessage,
  OutputFile,
  SocketMessage,
  UploadedItem
} from "../types";

const MAX_EVENTS = 120;
// 重连退避：2s 起步指数增长，上限 60s，加随机抖动避免服务恢复时雪崩重连
const RECONNECT_BASE_DELAY_MS = 2000;
const RECONNECT_MAX_DELAY_MS = 60000;
// 丢件主动补发（seq 跳号时断开重连触发差量补发）的最大连续尝试次数，
// 防止事件流被服务端裁剪后陷入"补发→再跳号→再补发"循环
const MAX_RESYNC_ATTEMPTS = 3;

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
  // 已收到的最大事件序号：重连时经 last_seq 交给服务端做差量补发
  const lastSeqRef = useRef<number | undefined>(undefined);
  // 常规断线重连的退避计数（连接成功后清零）
  const retryCountRef = useRef(0);
  // 丢件触发的主动补发标记：此类重连立即执行，不走退避
  const resyncPendingRef = useRef(false);
  const resyncAttemptsRef = useRef(0);
  const [threadId, setThreadId] = useState(getStoredThreadId);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [sessionPath, setSessionPath] = useState("");
  const [result, setResult] = useState("");
  const [lastError, setLastError] = useState("");
  const [lastPongAt, setLastPongAt] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadedItems, setUploadedItems] = useState<UploadedItem[]>([]);

  const clearSocketTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = undefined;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = undefined;
    }
  }, []);

  const resetSession = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    setThreadId(nextThreadId);
    setEvents([]);
    setFiles([]);
    setSessionPath("");
    setResult("");
    setLastError("");
    setUploadedItems([]);
    uploadedNameSetRef.current.clear();
    setIsRunning(false);
    setIsCancelling(false);
  }, []);

  const refreshFiles = useCallback(async () => {
    if (!sessionPath) {
      return;
    }

    // 文件列表按 thread_id 在服务端定位会话目录，前端不再传递本地绝对路径
    const response = await listSessionFiles(threadId);
    if (response.error) {
      throw new Error(response.error);
    }
    setFiles(response.files || []);
  }, [sessionPath, threadId]);

  useEffect(() => {
    let disposed = false;

    // 新 thread 即新事件流，旧 seq 在新流中无意义
    lastSeqRef.current = undefined;

    async function connect() {
      clearSocketTimers();
      const hadSocket = Boolean(socketRef.current);
      socketRef.current?.close();
      setConnectionState(hadSocket ? "reconnecting" : "connecting");

      // 浏览器 WS 无法自定义请求头：先经请求头认证换 60 秒短时令牌，
      // 长期密钥不再出现在 URL（P1-2）；已收过事件时附带 last_seq
      // 供服务端做断线差量补发
      const params = new URLSearchParams();
      if (API_KEY) {
        try {
          const { token } = await fetchLinkToken();
          params.set("token", token);
        } catch {
          // 令牌服务不可用（如后端未升级）时回退兼容入口：
          // 查询参数密钥，已弃用，仅保底不断连
          params.set("api_key", API_KEY);
        }
      }
      if (lastSeqRef.current !== undefined) {
        params.set("last_seq", String(lastSeqRef.current));
      }
      if (disposed) {
        return;
      }
      const query = params.toString();
      const wsUrl = `${WS_BASE_URL}/ws/${encodeURIComponent(threadId)}${query ? `?${query}` : ""}`;
      const socket = new WebSocket(wsUrl);
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          return;
        }
        retryCountRef.current = 0;
        resyncPendingRef.current = false;
        setConnectionState("connected");
        setLastError("");
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
          }
        }, 25000);
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong") {
            setLastPongAt(new Date().toISOString());
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          // seq 处理：回放事件直接接受（补发批次自身可能因服务端裁剪跳号）；
          // 实时流 seq 跳号说明有丢件，断开重连触发差量补发
          if (typeof payload.seq === "number") {
            // 已决定重连补发：close() 到 onclose 生效之间到达的实时事件一律
            // 丢弃（重连后会按序补发）。若不丢弃，同一突发里每条事件都会
            // 各自消耗一次补发预算，3 条突发即烧穿上限（审查修复 R-1）
            if (resyncPendingRef.current && !payload.replay) {
              return;
            }

            const lastSeq = lastSeqRef.current;
            if (payload.replay) {
              lastSeqRef.current = Math.max(lastSeq ?? 0, payload.seq);
            } else if (lastSeq !== undefined) {
              if (payload.seq <= lastSeq) {
                // 补发与实时窗口重叠的重复事件，丢弃
                return;
              }
              if (payload.seq > lastSeq + 1 && resyncAttemptsRef.current < MAX_RESYNC_ATTEMPTS) {
                resyncAttemptsRef.current += 1;
                resyncPendingRef.current = true;
                socket.close();
                return;
              }
              lastSeqRef.current = payload.seq;
            } else {
              lastSeqRef.current = payload.seq;
            }
            // 收到连续实时事件说明流已恢复，重置主动补发预算
            if (!payload.replay) {
              resyncAttemptsRef.current = 0;
            }
          }

          setEvents((previous) => [...previous, payload].slice(-MAX_EVENTS));

          if (payload.event === "session_created") {
            const path = extractString(payload.data, "path");
            if (path) {
              setSessionPath(path);
            }
          }

          if (payload.event === "task_result") {
            const finalResult = extractString(payload.data, "result");
            setResult(finalResult || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "task_cancelled") {
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }

          if (payload.event === "error") {
            setLastError(payload.message);
            setIsRunning(false);
            setIsCancelling(false);
          }
        } catch (error) {
          setLastError(error instanceof Error ? error.message : "WebSocket 消息解析失败");
        }
      };

      socket.onerror = () => {
        if (!disposed && socketRef.current === socket) {
          setLastError("WebSocket 连接异常，请确认后端服务已启动");
        }
      };

      socket.onclose = () => {
        if (socketRef.current !== socket) {
          return;
        }
        clearSocketTimers();
        if (disposed) {
          setConnectionState("closed");
          return;
        }
        setConnectionState("reconnecting");
        if (resyncPendingRef.current) {
          // 丢件触发的主动补发：立即带 last_seq 重连做差量补发
          reconnectTimerRef.current = window.setTimeout(connect, 0);
        } else {
          // 常规断线：指数退避 + 随机抖动，防止服务恢复瞬间雪崩重连
          const delay = Math.min(
            RECONNECT_BASE_DELAY_MS * 2 ** retryCountRef.current,
            RECONNECT_MAX_DELAY_MS
          ) + Math.random() * 1000;
          retryCountRef.current += 1;
          reconnectTimerRef.current = window.setTimeout(connect, delay);
        }
      };
    }

    connect();

    return () => {
      disposed = true;
      clearSocketTimers();
      socketRef.current?.close();
    };
  }, [clearSocketTimers, threadId]);

  useEffect(() => {
    if (!sessionPath) {
      return;
    }

    refreshFiles().catch((error: unknown) => {
      setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
    });

    const timer = window.setInterval(() => {
      refreshFiles().catch((error: unknown) => {
        setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
      });
    }, isRunning ? 2500 : 6000);

    return () => window.clearInterval(timer);
  }, [isRunning, refreshFiles, sessionPath]);

  const submitTask = useCallback(
    async (query: string) => {
      const cleanQuery = query.trim();
      if (!cleanQuery) {
        throw new Error("请输入研搜任务");
      }

      setIsRunning(true);
      setIsCancelling(false);
      setEvents([]);
      setResult("");
      setLastError("");
      // 新任务的执行轨迹从零开始收集：重置 seq 基线，
      // 避免服务端重启（seq 重新从小值自增）后新事件被当作重复丢弃
      lastSeqRef.current = undefined;
      try {
        const response = await startTask(cleanQuery, threadId);
        if (response.thread_id && response.thread_id !== threadId) {
          storeThreadId(response.thread_id);
          setThreadId(response.thread_id);
        }
        return response;
      } catch (error) {
        setIsRunning(false);
        setIsCancelling(false);
        throw error;
      }
    },
    [threadId]
  );

  const cancelCurrentTask = useCallback(async () => {
    if (!isRunning) {
      throw new Error("当前没有正在执行的任务");
    }

    setIsCancelling(true);
    setLastError("");
    try {
      const response = await cancelTask(threadId);
      if (response.status === "cancelled") {
        setIsRunning(false);
        setIsCancelling(false);
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, [isRunning, threadId]);

  const uploadFiles = useCallback(
    async (items: UploadedItem[]) => {
      if (items.length === 0) {
        throw new Error("请选择要上传的文件");
      }

      const nextItems = items.filter((item) => !uploadedNameSetRef.current.has(item.name));

      if (nextItems.length === 0) {
        return {
          status: "uploaded",
          files: Array.from(uploadedNameSetRef.current)
        };
      }

      setIsUploading(true);
      setLastError("");
      try {
        const response = await uploadSessionFiles(
          nextItems.map((item) => item.raw),
          threadId
        );
        setUploadedItems((previous) => {
          const names = new Set(previous.map((item) => item.name));
          const next = [...previous];
          nextItems.forEach((item) => {
            if (!names.has(item.name)) {
              names.add(item.name);
              uploadedNameSetRef.current.add(item.name);
              next.push(item);
            }
          });
          return next;
        });
        return response;
      } finally {
        setIsUploading(false);
      }
    },
    [threadId]
  );

  const stats = useMemo(() => {
    const toolEvents = events.filter((event) => event.event === "tool_start").length;
    const assistantEvents = events.filter((event) => event.event === "assistant_call").length;
    const errorEvents = events.filter((event) => event.event === "error").length;

    return {
      toolEvents,
      assistantEvents,
      errorEvents,
      fileCount: files.length
    };
  }, [events, files.length]);

  return {
    connectionState,
    events,
    files,
    isCancelling,
    isRunning,
    isUploading,
    lastError,
    lastPongAt,
    refreshFiles,
    resetSession,
    result,
    sessionPath,
    stats,
    cancelCurrentTask,
    submitTask,
    threadId,
    uploadFiles,
    uploadedItems
  };
}
