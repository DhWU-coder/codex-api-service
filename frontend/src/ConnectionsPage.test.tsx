import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionsPage } from "./ConnectionsPage";
import type { AdminConnections } from "./types";

// 使用与页面所在设备无关的服务端地址，并用非默认端口防止实现写死 1219。
const addresses = (host: string) => ({
  host, api: `http://${host}:9876/v1`, console: `http://${host}:9876/ui`,
  health: `http://${host}:9876/health`, models: `http://${host}:9876/v1/models`
});
const snapshot: AdminConnections = {
  listenHost: "0.0.0.0", port: 9876,
  local: addresses("127.0.0.1"), localNote: null,
  lan: addresses("10.0.0.10"), lanNote: null
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("connection ports page", () => {
  it("shows and copies each local and LAN endpoint with authentication", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot)));
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<ConnectionsPage apiKey="local-secret" />);
    await screen.findByText("10.0.0.10");
    expect(fetch).toHaveBeenCalledWith("/admin/connections", { headers: { Authorization: "Bearer local-secret" } });
    for (const [scope, group] of [["本地", snapshot.local!], ["局域网", snapshot.lan!]] as const) {
      const card = screen.getByRole("region", { name: `${scope}连接` });
      expect(within(card).getByText("9876")).toBeTruthy();
      for (const [key, title] of [["api", "API Base URL"], ["console", "控制台"], ["health", "健康检查"], ["models", "模型列表"]] as const) {
        expect(within(card).getByText(group[key])).toBeTruthy();
        fireEvent.click(within(card).getByRole("button", { name: `复制${scope} ${title}` }));
        await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(group[key]));
        expect(await screen.findByText(`已复制${scope} ${title}`)).toBeTruthy();
      }
    }
  });

  it.each([
    "当前仅允许本机连接，尚未开放局域网监听。",
    "未检测到局域网 IP，请检查服务电脑的网络连接。"
  ])("shows an unavailable LAN reason without a fake copy target: %s", async (note) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...snapshot, lan: null, lanNote: note }))));
    render(<ConnectionsPage apiKey="" />);
    expect(await screen.findByText(note)).toBeTruthy();
    const card = screen.getByRole("region", { name: "局域网连接" });
    expect(within(card).queryByRole("button")).toBeNull();
    expect(screen.getAllByRole("button", { name: /^复制/ })).toHaveLength(4);
  });

  it("refreshes the IP and rereads when the access key changes", async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(snapshot)));
    vi.stubGlobal("fetch", fetch);
    const { rerender } = render(<ConnectionsPage apiKey="first" />);
    await screen.findByText("http://10.0.0.10:9876/v1");
    fetch.mockImplementation(async () => new Response(JSON.stringify({ ...snapshot, lan: addresses("10.0.0.20") })));
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("http://10.0.0.20:9876/v1")).toBeTruthy();
    expect(screen.queryByText("http://10.0.0.10:9876/v1")).toBeNull();
    rerender(<ConnectionsPage apiKey="second" />);
    await waitFor(() => expect(fetch).toHaveBeenLastCalledWith("/admin/connections", { headers: { Authorization: "Bearer second" } }));
    expect(await screen.findByText("http://10.0.0.20:9876/v1")).toBeTruthy();
  });

  it("shows a load failure and can retry", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ error: { message: "请填写访问密钥" } }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(snapshot)));
    vi.stubGlobal("fetch", fetch);
    render(<ConnectionsPage apiKey="" />);
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "连接地址读取失败：请填写访问密钥");
    expect(screen.queryByRole("button", { name: /^复制/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("10.0.0.10")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows copy failure when the browser rejects copying", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot))));
    vi.stubGlobal("navigator", {});
    render(<ConnectionsPage apiKey="" />);
    fireEvent.click(await screen.findByRole("button", { name: "复制局域网 API Base URL" }));
    expect(await screen.findByText("复制失败，请手动选择地址复制。")).toBeTruthy();
    expect(screen.queryByText("已复制局域网 API Base URL")).toBeNull();
  });
});
