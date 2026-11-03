"""远端能不能看到数字人视频？先看本机 WebRTC 能否拿到公网候选地址。

背景：数字人画面走 WebRTC（aiortc）。LiveTalking 默认配了 STUN
（config.py --stun，默认 stun:stun.freeswitch.org:3478），所以服务端除了局域网
候选，还会拿到一个「公网 IP:端口」候选 —— 有这个，远端观众才可能靠 NAT 打洞直连；
只有局域网候选时远端必然收不到画面（得配 TURN 或改用 HTTP-FLV）。

用法：
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\smoke\\_probe_webrtc_ice.py
    $env:AILIVE_STUN = "stun:stun.l.google.com:19302"   # 换别的 STUN 再试
"""

import asyncio
import os
import re
import sys

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection

STUN = os.environ.get("AILIVE_STUN", "stun:stun.freeswitch.org:3478")
PRIVATE_PREFIX = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                  "172.2", "172.3", "127.", "169.254.")


async def gather(stun: str) -> str:
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[RTCIceServer(urls=stun)]))
    try:
        # 必须有媒体轨道/收发器，aiortc 才会去收集候选地址
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        await pc.setLocalDescription(await pc.createOffer())
        for _ in range(80):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.25)
        return pc.localDescription.sdp
    finally:
        await pc.close()


def main() -> int:
    print(f"STUN 服务器：{STUN}")
    try:
        sdp = asyncio.run(gather(STUN))
    except Exception as exc:  # noqa: BLE001 - 探测脚本，任何异常都当失败信息
        print(f"收集候选失败：{type(exc).__name__}: {exc}")
        return 1

    candidates = re.findall(r"^a=candidate:.*$", sdp, flags=re.M)
    host, srflx = [], []
    print(f"\n候选地址共 {len(candidates)} 条")
    for line in candidates:
        parts = line.split()
        address, port = parts[4], parts[5]
        kind = parts[7] if len(parts) > 7 else "?"
        print(f"  [{kind}] {address}:{port}")
        if kind == "host":
            host.append(f"{address}:{port}")
        elif kind in ("srflx", "prflx"):
            srflx.append(f"{address}:{port}")

    public = [item for item in srflx if not item.startswith(PRIVATE_PREFIX)]
    print(f"\n局域网候选：{host or '（无）'}")
    print(f"公网候选(srflx)：{srflx or '（无）'}")
    print("-" * 60)
    if public:
        print("结论：STUN 通了，能拿到公网候选 → 远端观众有打洞成功的可能。")
        print("      但对称 NAT / 运营商大内网仍会失败；要「保证」远端能看到画面，")
        print("      需要 TURN 服务器，或改用 HTTP-FLV / HLS 播放（走 HTTPS 隧道没问题）。")
        return 0
    if srflx:
        print("结论：拿到 srflx 但仍是私网地址，STUN 可能被劫持或失效，建议换一个 STUN 再试。")
        return 1
    print("结论：只有局域网候选 → 远端观众必然收不到视频，必须配 TURN 或改用 HTTP-FLV。")
    return 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())