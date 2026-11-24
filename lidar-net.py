#!/usr/bin/env python3
"""Set up a dedicated NetworkManager Ethernet port and two Airy + one Fairy."""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode

HOST = "192.168.1.102"
FACTORY = "192.168.1.200"
TARGETS = ["192.168.1." + str(n) for n in (201, 202, 203)]
NETWORK = [FACTORY] + TARGETS
MAC_RE = r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(*args, check=True):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, check=check).stdout.strip()


def ip(*args):
    return json.loads(run("ip", "-j", *args))


def arp_lookup(iface, address, source):
    """Broadcast twice and collect *all* replies, including duplicate-IP devices."""
    hw = bytes.fromhex(ip("link", "show", "dev", iface)[0]["address"].replace(":", ""))
    target = socket.inet_aton(address)
    sender = socket.inet_aton(source)
    header = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    packet = b"\xff" * 6 + hw + b"\x08\x06" + header + hw + sender + bytes(6) + target
    found = set()
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as sock:
        sock.bind((iface, 0))
        for _ in range(2):
            sock.send(packet)
            deadline = time.monotonic() + .65
            while time.monotonic() < deadline:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                try:
                    data = sock.recv(2048)
                except socket.timeout:
                    break
                if (len(data) >= 42 and data[12:20] == b"\x08\x06\x00\x01\x08\x00\x06\x04"
                        and data[20:22] == b"\x00\x02" and data[28:32] == target
                        and data[32:38] == hw and data[38:42] == sender and data[22:28] != hw):
                    found.add(data[22:28].hex(":"))
    return sorted(found)


@contextmanager
def pinned(iface, address, mac):
    old = ip("neigh", "show", "to", address, "dev", iface)
    permanent = next((n for n in old if "PERMANENT" in n.get("state", [])), None)
    if permanent:
        require(permanent.get("lladdr") == mac, "已有不同的永久邻居绑定 " + address)
        yield
        return
    run("ip", "neigh", "replace", address, "lladdr", mac, "nud", "permanent", "dev", iface)
    try:
        yield
    finally:
        run("ip", "neigh", "del", address, "dev", iface, check=False)


def web(address, page, form=None):
    conn = http.client.HTTPConnection(address, timeout=3, source_address=(HOST, 0))
    try:
        if form is None:
            conn.request("GET", "/" + page)
        else:
            conn.request("POST", "/" + page, urlencode(form),
                         {"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        require(response.status == 200, "{} {} HTTP {}".format(address, page, response.status))
        return json.loads(response.read(65536)) if form is None else None
    finally:
        conn.close()


def identity(address, mac):
    dev = web(address, "device_data.json")
    require(dev["mac_addr"].lower() == mac and dev["sn"] and dev["lidar_model"] in ("Airy", "Fairy"),
            "设备身份不匹配 " + address + " " + mac)
    return dev


def assign(devices, first_mac=None):
    """Prefer established roles so an interrupted configuration resumes identically."""
    airys = sorted((d for d in devices if d["device"]["lidar_model"] == "Airy"), key=lambda d: d["mac"])
    fairys = [d for d in devices if d["device"]["lidar_model"] == "Fairy"]
    require(len(airys) == 2 and len(fairys) == 1 and len({d["mac"] for d in devices}) == 3
            and len({d["device"]["sn"] for d in devices}) == 3, "需要两台 Airy 和一台 Fairy，身份必须唯一")
    for pair in (airys, airys[::-1]):
        order = pair + fairys
        if (not first_mac or pair[0]["mac"] == first_mac) and all(
                d["address"] in (FACTORY, target) for d, target in zip(order, TARGETS)):
            return order
    raise ValueError("已占用的目标地址与期望角色冲突，请核对接线、IP 或 --airy1-mac")


def desired(settings, i):
    for field in ("SrcIp", "DstIp", "MPort", "DPort", "IPort", "ResDefault", "GroupSelect",
                  "PulseStart", "PulseWidth", "PulseStep", "TriggerMode"):
        require(field in settings, "固件设置中缺少 " + field)
    require(settings["ResDefault"] == "0" and settings["GroupSelect"] in ("0", "1")
            and settings["TriggerMode"] in ("0", "1"), "不支持此固件表单值")
    result = dict(settings)
    result.update(SrcIp=TARGETS[i], DstIp=HOST, MPort=str(6701+i),
                  DPort=str(7801+i), IPort=str(6801+i))
    return result


def save_form(settings):
    form = dict(settings, save_param="Save", first_start_angle=settings["PulseStart"],
                first_pulse_width=settings["PulseWidth"], first_pulse_step=settings["PulseStep"])
    if settings["GroupSelect"] == "1":
        form["first_group_switch"] = "ON"
    if settings["TriggerMode"] == "0":
        form["mode2"] = "Mode2"
    else:
        form["mode1"] = "Mode1(+25%)"
    return form


def configure_device(iface, d, expected):
    old, new, mac = d["address"], expected["SrcIp"], d["mac"]
    with pinned(iface, old, mac):
        require(identity(old, mac) == d["device"] and web(old, "setting_data.json") == d["settings"],
                "设备或参数在预检后发生变化 " + mac)
        if expected == d["settings"]:
            print(new, mac, "已符合配置")
            return
        try:
            web(old, "Parameter_Setting.html", save_form(expected))
        except (OSError, http.client.HTTPException) as err:
            print("地址切换期间 HTTP 连接中断，正在回读：", err)
    for attempt in range(12):
        try:
            with pinned(iface, new, mac):
                require(identity(new, mac)["sn"] == d["device"]["sn"], "序列号改变")
                actual = web(new, "setting_data.json")
            changed = sorted(k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k))
            require(not changed, "回读参数不一致 " + mac + " " + str(changed))
            print(new, mac, "身份及全部参数核对通过")
            return
        except (OSError, http.client.HTTPException):
            if attempt == 11:
                raise
            time.sleep(1)


def guard_host(iface):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", iface), "网口名称无效")
    link = ip("link", "show", "dev", iface)[0]
    require(link["link_type"] == "ether" and "master" not in link, "需要独立以太网接口")
    for family in ("-4", "-6"):
        defaults = ip(family, "route", "show", "table", "all", "default")
        require(not any(r.get("dst") == "default" and (r.get("dev") == iface or any(
            h.get("dev") == iface for h in r.get("nexthops", []))) for r in defaults),
            "指定网口承载默认路由 " + iface)
    peer = os.environ.get("SSH_CONNECTION", "").split()
    if peer:
        require(ip("route", "get", str(ipaddress.ip_address(peer[0])))[0].get("dev") != iface,
                "指定网口承载当前 SSH 连接")
    has_addr = False
    for dev in ip("-4", "address", "show"):
        for a in dev["addr_info"]:
            match = a["local"] == HOST and a["prefixlen"] == 32
            if dev["ifname"] == iface:
                require(match or ipaddress.ip_address(a["local"]).is_link_local, "指定网口已有其他 IPv4 配置")
                has_addr |= match
            else:
                require(a["local"] != HOST, "接收地址已绑定其他接口")
    return "UP" in link["flags"], has_addr


def profile_uuid(name, iface):
    rows = run("nmcli", "-t", "-f", "NAME,UUID", "con", "show").splitlines()
    uuids = [line.rsplit(":", 1)[1] for line in rows if line.startswith(name + ":")]
    require(len(uuids) <= 1, "NetworkManager 配置重名 " + name)
    if uuids:
        require(run("nmcli", "-g", "connection.interface-name", "con", "show", "uuid", uuids[0]) == iface,
                "同名配置属于其他接口")
    return uuids[0] if uuids else None


def persist(iface, name, uuid):
    fields = ["ipv4.method", "manual", "ipv4.addresses", HOST + "/32", "ipv4.gateway", "",
              "ipv4.routes", ",".join(s + "/32" for s in TARGETS), "ipv4.never-default", "yes",
              "ipv6.method", "disabled", "connection.autoconnect", "yes",
              "connection.autoconnect-priority", "100"]
    if uuid:
        run("nmcli", "con", "modify", "uuid", uuid, *fields)
    else:
        run("nmcli", "con", "add", "type", "ethernet", "ifname", iface, "con-name", name, *fields)
        uuid = profile_uuid(name, iface)
    active = run("nmcli", "-t", "-f", "UUID,DEVICE", "con", "show", "--active").splitlines()
    if uuid + ":" + iface in active:
        try:
            run("nmcli", "device", "reapply", iface)
        except subprocess.CalledProcessError:
            run("nmcli", "con", "up", "uuid", uuid, "ifname", iface)
    else:
        run("nmcli", "con", "up", "uuid", uuid, "ifname", iface)
    for address in TARGETS:
        route = ip("route", "get", address)[0]
        require(route.get("dev") == iface and route.get("prefsrc") == HOST, "持久化后路由不匹配 " + address)


def execute(args):
    iface = args.iface
    require(os.geteuid() == 0, "请用 sudo 运行")
    require(shutil.which("ip") and shutil.which("nmcli"), "需要 iproute2 和 NetworkManager")
    lock = open("/run/starpilot-lidar-network.lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    up, assigned = guard_host(iface)
    require(not any(a.get("scope") == "global" for dev in ip("-6", "addr", "show", "dev", iface)
                    for a in dev.get("addr_info", [])), "指定接口已有全局 IPv6 地址")
    name = "starpilot-lidar-" + iface
    uuid = profile_uuid(name, iface)
    # Temporary setup, including the conflicting factory address, is removed on exit.
    with ExitStack() as temp:
        if not up:
            run("ip", "link", "set", iface, "up")
            temp.callback(run, "ip", "link", "set", iface, "down", check=False)
            time.sleep(2)
        require(not arp_lookup(iface, HOST, "0.0.0.0"), "雷达网上另有设备占用 " + HOST)
        if not assigned:
            run("ip", "addr", "add", HOST + "/32", "dev", iface)
            temp.callback(run, "ip", "addr", "del", HOST + "/32", "dev", iface, check=False)
        devices = []
        for address in NETWORK:
            routes = ip("route", "show", "exact", address + "/32")
            if not routes:
                run("ip", "route", "add", address + "/32", "dev", iface, "src", HOST)
                temp.callback(run, "ip", "route", "del", address + "/32", "dev", iface, check=False)
            require(ip("route", "get", address)[0].get("dev") == iface, "目标已路由至其他接口 " + address)
            macs = arp_lookup(iface, address, HOST)
            require(address == FACTORY or len(macs) <= 1, "目标 IP 存在冲突 " + address)
            for mac in macs:
                with pinned(iface, address, mac):
                    dev = identity(address, mac)
                    cfg = web(address, "setting_data.json")
                require(cfg["SrcIp"] == address, "设备报告的地址不符")
                devices.append(dict(address=address, mac=mac, device=dev, settings=cfg))
        order = assign(devices, args.airy1_mac)
        plans = [desired(d["settings"], i) for i, d in enumerate(order)]
        for d, p in zip(order, plans):
            print(d["device"]["lidar_model"], d["device"]["sn"], d["mac"], d["address"], "->",
                  p["SrcIp"], p["DstIp"], p["MPort"], p["DPort"], p["IPort"], flush=True)
        if args.preview:
            return
        base = Path("/var/lib/starpilot/lidar-network")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=iface + "-", dir=str(base)))
        (backup/"devices.json").write_text(json.dumps(order, indent=2))
        (backup/"nm-before.txt").write_text(run("nmcli", "device", "show", iface) + "\n" +
            (run("nmcli", "con", "show", "uuid", uuid) if uuid else ""))
        print("本次写入前备份", backup, flush=True)
        for d, p in zip(order, plans):
            configure_device(iface, d, p)
    persist(iface, name, uuid)
    for d, p in zip(order, plans):
        require(identity(p["SrcIp"], d["mac"])["sn"] == d["device"]["sn"] and
                web(p["SrcIp"], "setting_data.json") == p, "持久化后设备回读失败")
    print("已持久化并验证", name, HOST + "/32", *TARGETS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iface", default="eth0", help="dedicated LiDAR port (default eth0)")
    parser.add_argument("--preview", action="store_true", help="discover and print plan; temporary networking only")
    parser.add_argument("--airy1-mac", type=str.lower, help="select which Airy should use .201")
    args = parser.parse_args()
    if args.airy1_mac and not re.fullmatch(MAC_RE, args.airy1_mac):
        parser.error("invalid --airy1-mac")
    try:
        execute(args)
    except (ValueError, KeyError, OSError, http.client.HTTPException, subprocess.CalledProcessError) as err:
        sys.exit("配置停止：" + str(err))


if __name__ == "__main__":
    main()
