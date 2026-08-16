# 香橙派 Zero 3 读取米家温湿度计 3 / 3 Mini

这是一个独立的 BLE 广播采集器。它直接使用香橙派的蓝牙适配器读取 MiBeacon 广播，不需要 Home Assistant、小米中枢网关或小米云。

支持的设备：

- `MJWSD05MMC`（米家/小米智能温湿度计 3），产品 ID `0x2832`、`0x4C47`
- `MJWSD06MMC`（米家/小米智能温湿度计 3 Mini），产品 ID `0x55B5`、`0x5BEA`

每次解析到变化后的温度或湿度数据时，程序会：

- 输出到终端
- 如果配置了 webhook，发送一次 HTTP POST JSON

## 系统要求

- Orange Pi Zero 3
- 64 位 Armbian/Debian Bookworm 或更新版本
- Python 3.11+
- BlueZ 可正常识别蓝牙适配器

先检查蓝牙：

```bash
sudo apt update
sudo apt install -y bluetooth bluez python3 python3-venv python3-pip
sudo systemctl enable --now bluetooth
sudo rfkill unblock bluetooth
bluetoothctl show
```

## 安装

```bash
cd orange_pi_mijia_monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

也可以使用 uv：

```bash
uv venv --python /usr/bin/python3 --no-managed-python
source .venv/bin/activate
uv pip install -r requirements.txt
```

## JSON 配置

复制示例配置：

```bash
cp config.example.json config.json
```

`config.json` 格式：

```json
{
  "mac": "A4:C1:38:12:34:56",
  "bindkey": "00112233445566778899aabbccddeeff",
  "adapter": null,
  "timeout": 600,
  "once": false,
  "json": false,
  "raw": false,
  "verbose": false,
  "webhook": {
    "url": "https://example.com/mijia-webhook",
    "timeout": 10,
    "headers": {
      "Authorization": "Bearer replace-me"
    }
  }
}
```

配置字段：

- `mac`：只接收指定设备；设为 `null` 时接收所有支持的设备
- `bindkey`：与设备 MAC 对应的 32 位 MiBeacon bindkey
- `adapter`：BlueZ 适配器，例如 `hci0`；`null` 表示使用系统默认值
- `timeout`：扫描秒数；`0` 表示一直运行
- `once`：收到同时包含温度和湿度的数据后退出
- `json`：终端使用 JSON Lines 输出
- `raw`：向 stderr 显示目标设备原始 MiBeacon payload
- `verbose`：启用依赖库日志
- `webhook.url`：接收数据的 HTTP/HTTPS 地址；`null` 或空字符串表示关闭 webhook
- `webhook.timeout`：单次 webhook 请求超时秒数
- `webhook.headers`：随请求发送的自定义 HTTP 请求头，可用于鉴权

`config.json` 已加入 `.gitignore`，不要提交真实密钥或 webhook 凭据。

## bindkey

已绑定米家 App 的原厂固件通常使用加密 MiBeacon 广播。bindkey 是 16 字节密钥，文本形式为 32 个十六进制字符。

将密钥直接填写到 `config.json` 的 `bindkey` 字段。bindkey 必须与设备 MAC 对应，重新绑定设备后可能需要重新提取密钥。

## 运行

默认读取当前目录的 `config.json`：

```bash
python mijia_monitor.py
```

指定其他配置文件：

```bash
python mijia_monitor.py --config /etc/mijia-monitor/config.json
```

发现设备后，stderr 会显示：

```text
[mijia-monitor] found MJWSD06MMC at A4:C1:38:12:34:56, product_id=0x55B5
```

成功解析后，stdout 会显示：

```text
2026-08-14T18:00:00+08:00  MJWSD06MMC  A4:C1:38:12:34:56  temperature=23.4 C  humidity=56 %  rssi=-55 dBm
```

原厂固件可能数分钟才广播一次测量数据，建议 `timeout` 至少设为 `600`。

## Webhook 请求

每次 Collector 生成一条新的 `Reading` 时，程序都会向 `webhook.url` 发送：

```http
POST /mijia-webhook
Content-Type: application/json
Authorization: Bearer replace-me
```

JSON body 示例：

```json
{
  "timestamp": "2026-08-14T18:00:00+08:00",
  "address": "A4:C1:38:12:34:56",
  "model": "MJWSD06MMC",
  "product_id": "0x55B5",
  "temperature": 23.4,
  "humidity": null,
  "battery": null,
  "rssi": -55.0
}
```

温度和湿度可能分帧到达，因此某些字段可以是 `null`。重复广播不会重复发送 webhook；只有 Collector 生成变化后的读数时才发送。

Webhook 请求失败只会写入 stderr，不会终止蓝牙扫描：

```text
[mijia-monitor] webhook failed: ...
```

## 常见问题

查看适配器状态：

```bash
rfkill list bluetooth
bluetoothctl show
systemctl status bluetooth --no-pager
```

若提示 `org.bluez.Error.NotReady`：

```bash
sudo rfkill unblock bluetooth
sudo bluetoothctl power on
```

## 离线测试

```bash
python -m unittest -v
```

测试覆盖未加密广播、AES-CCM 加密广播、3 Mini 产品 ID、JSON 配置加载和 webhook JSON 请求。

## PHP 接收服务

MySQL webhook 接收和查询 API 位于 [`php_webhook_service/`](php_webhook_service/README.md)。

## TODO

- [ ] 打通端到端多设备支持
  - 将 JSON 配置改为设备数组，每台设备独立配置 MAC 和 bindkey
  - Collector 按 MAC 使用对应的解密密钥和解析状态
  - Webhook 保持携带设备地址和型号
  - 查询 API 增加设备列表接口及设备过滤
  - 前端增加设备选择器，避免温度和湿度来自不同设备
