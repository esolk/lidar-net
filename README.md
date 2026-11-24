# lidar-net

在配有两台 RoboSense Airy 和一台 Fairy 的 Linux 工控机上配置雷达网络。每辆车使用独立的雷达网段。需要 Python 3.8 或以上、iproute2、NetworkManager 和本机 sudo 权限。

在目标车终端运行以下命令，匿名下载固定版本并在下载成功后执行。将 `eth0` 替换为连接雷达交换机的专用网口。

```bash
( f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL https://raw.githubusercontent.com/esolk/lidar-net/v1.0.0/lidar-net.py -o "$f" && sudo python3 -I "$f" --iface eth0 )
```

已有脚本时，可先预览再正式执行。

```bash
sudo python3 -I lidar-net.py --iface eth0 --preview
sudo python3 -I lidar-net.py --iface eth0
```

预览会临时配置网络以识别雷达，并列出计划。正式执行会在本机备份固件参数，按 MAC 与序列号确认设备，写入并回读配置，通过 NetworkManager 持久化接收地址及三条专用路由。

| 设备 | IPv4 | MSOP / DIFOP / IMU |
|---|---|---|
| 接收网口 | 192.168.1.102/32 | — |
| Airy 1 | 192.168.1.201 | 6701 / 7801 / 6801 |
| Airy 2 | 192.168.1.202 | 6702 / 7802 / 6802 |
| Fairy | 192.168.1.203 | 6703 / 7803 / 6803 |

支持三台处于相同的出厂地址 192.168.1.200，或部分设备已经配置的情况。首次按 MAC 排序区分两台 Airy。如需将某个安装位置固定为 Airy 1，追加 `--airy1-mac <该台设备的MAC>`。本机备份位于 `/var/lib/starpilot/lidar-network/`。
