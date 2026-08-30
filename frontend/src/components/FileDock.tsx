import {
  DownloadOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileTextOutlined,
  ReloadOutlined
} from "@ant-design/icons";
import { Button, Empty, Tooltip, message } from "antd";
import { downloadSessionFile } from "../lib/api";
import type { OutputFile } from "../types";

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatMtime(value: number): string {
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) {
    return "未知时间";
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function FileIcon({ name }: { name: string }) {
  if (name.endsWith(".pdf")) {
    return <FilePdfOutlined aria-hidden />;
  }
  if (name.endsWith(".md")) {
    return <FileMarkdownOutlined aria-hidden />;
  }
  return <FileTextOutlined aria-hidden />;
}

interface FileDockProps {
  files: OutputFile[];
  onRefresh: () => void;
  sessionPath: string;
}

export function FileDock({ files, onRefresh, sessionPath }: FileDockProps) {
  // fetch + 请求头 + blob 下载：密钥不出现在 URL（P1-2），
  // 失败时经 antd message 提示（如令牌/密钥失效、文件被清理）
  const handleDownload = async (file: OutputFile) => {
    try {
      await downloadSessionFile(file.thread_id, file.path);
    } catch (error) {
      const detail = error instanceof Error ? error.message : "下载失败";
      void message.error(`下载 ${file.name} 失败：${detail}`);
    }
  };

  return (
    <section className="console-panel file-panel" aria-labelledby="file-title">
      <div className="panel-heading">
        <div>
          <span className="panel-kicker">ARTIFACTS</span>
          <h2 id="file-title">输出文件</h2>
        </div>
        <Tooltip title="刷新文件列表">
          <Button
            aria-label="刷新文件列表"
            className="icon-button"
            icon={<ReloadOutlined />}
            onClick={onRefresh}
            shape="circle"
          />
        </Tooltip>
      </div>

      {sessionPath ? <p className="path-readout">{sessionPath}</p> : null}

      {files.length === 0 ? (
        <div className="compact-empty">
          <Empty description="暂无输出产物" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        </div>
      ) : (
        <ul className="file-list">
          {files.map((file) => (
            <li className="file-item" key={file.path}>
              <div className="file-icon">
                <FileIcon name={file.name} />
              </div>
              <div className="file-copy">
                <strong title={file.name}>{file.name}</strong>
                <span>
                  {formatBytes(file.size)} · {formatMtime(file.mtime)}
                </span>
              </div>
              <Tooltip title="下载">
                <Button
                  aria-label={`下载 ${file.name}`}
                  className="icon-button"
                  icon={<DownloadOutlined />}
                  onClick={() => void handleDownload(file)}
                  shape="circle"
                />
              </Tooltip>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
