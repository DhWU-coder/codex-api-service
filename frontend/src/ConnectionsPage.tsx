import { AlertTriangle, Check, Copy, Monitor, Network, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import { fetchAdminConnections } from "./api";
import { copyConnectionAddress } from "./clipboard";
import type { AdminConnections, ConnectionAddresses } from "./types";

function CopyAddressButton({ label, value }: { label: string; value: string }) {
  const [state, setState] = useState<"idle" | "copying" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state !== "copied") return;
    // 成功提示短暂保留，切换地址或离开页面时清理计时器。
    const timeout = window.setTimeout(() => setState("idle"), 2000);
    return () => window.clearTimeout(timeout);
  }, [state]);

  return (
    <div className="connection-copy">
      <button
        type="button"
        className="secondary-button"
        aria-label={`复制${label}`}
        disabled={state === "copying"}
        onClick={async () => {
          setState("copying");
          try {
            await copyConnectionAddress(value);
            setState("copied");
          } catch {
            setState("failed");
          }
        }}
      >
        {state === "copied" ? <Check size={15} /> : <Copy size={15} />}
        {state === "copied" ? "已复制" : "复制"}
      </button>
      <span className="connection-copy-note" role="status">
        {state === "copied" ? `已复制${label}` : state === "failed" ? "复制失败，请手动选择地址复制。" : ""}
      </span>
    </div>
  );
}

function ConnectionGroup({ scope, addresses, note, port }: {
  scope: "本地" | "局域网";
  addresses: ConnectionAddresses | null;
  note: string | null;
  port: number;
}) {
  // 入口标签固定，地址和端口全部采用服务端快照，禁止根据当前浏览器推测局域网 IP。
  const rows = [
    ["api", "API Base URL"], ["console", "控制台"], ["health", "健康检查"], ["models", "模型列表"]
  ] as const;
  return (
    <section className="connection-card" aria-label={`${scope}连接`}>
      <header>
        {scope === "本地" ? <Monitor size={20} /> : <Network size={20} />}
        <h3>{scope}连接</h3>
      </header>
      {addresses ? (
        <>
          <p className="connection-description">{scope === "本地" ? "供服务所在电脑使用。" : "供与服务电脑网络互通的其他设备使用。"}</p>
          <dl className="connection-meta">
            <div><dt>IP / 主机</dt><dd>{addresses.host}</dd></div>
            <div><dt>端口</dt><dd>{port}</dd></div>
          </dl>
          <div className="connection-addresses">
            {rows.map(([key, title]) => (
              <div className="connection-address-row" key={key}>
                <div><span>{title}</span><code>{addresses[key]}</code></div>
                <CopyAddressButton label={`${scope} ${title}`} value={addresses[key]} />
              </div>
            ))}
          </div>
        </>
      ) : <p className="connection-description">{note || "连接地址暂不可用，请刷新重试。"}</p>}
    </section>
  );
}

export function ConnectionsPage({ apiKey }: { apiKey: string }) {
  const [connections, setConnections] = useState<AdminConnections | null>(null);
  const [refreshIndex, setRefreshIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setConnections(null);
    setError("");
    // 每次进入、刷新和密钥变化都读取最新地址，忽略旧请求较晚到达的结果。
    void fetchAdminConnections(apiKey).then((snapshot) => {
      if (!cancelled) setConnections(snapshot);
    }).catch((caught) => {
      if (!cancelled) setError(caught instanceof Error ? caught.message : "连接地址读取失败");
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [apiKey, refreshIndex]);

  return (
    <section className="panel" aria-label="连接端口">
      <header className="toolbar">
        <div><h2>连接端口</h2><p>查看服务连接地址，并复制到其他服务中使用。</p></div>
        <button className="secondary-button" disabled={loading} onClick={() => setRefreshIndex((current) => current + 1)}>
          <RefreshCw size={16} />{loading ? "读取中…" : "刷新"}
        </button>
      </header>
      <div className="connections-page">
        <p className="connection-description">其他服务请填写 API Base URL；浏览器查看页面请使用控制台地址。</p>
        {loading ? <p role="status">正在读取连接地址…</p> : null}
        {error ? <div className="banner error" role="alert"><AlertTriangle size={18} />{error}</div> : null}
        {connections ? (
          <>
            <p className="connection-description">监听地址：<code>{connections.listenHost}</code> · 端口：<strong>{connections.port}</strong></p>
            <div className="connection-grid">
              <ConnectionGroup scope="本地" addresses={connections.local} note={connections.localNote} port={connections.port} />
              <ConnectionGroup scope="局域网" addresses={connections.lan} note={connections.lanNote} port={connections.port} />
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
