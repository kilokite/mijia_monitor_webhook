# 米家温湿度 Webhook PHP 服务

一个无框架 PHP 8 服务，用于：

- 接收 `mijia_monitor.py` 发出的 webhook
- 将数据保存到 MySQL
- 查询历史数据或最新数据

## 数据库默认配置

| 配置 | 默认值 |
|---|---|
| Host | `localhost` |
| Port | `3306` |
| Database | `zocs_playground` |
| Username | `zocs_playground` |
| Password | `zocs_playground` |

所有值都可以通过同名环境变量覆盖：`DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`。

## 安装

Debian/Ubuntu：

```bash
sudo apt update
sudo apt install -y php-cli php-mysql mysql-server
sudo mysql < schema.sql
```

`schema.sql` 会创建数据库、用户、数据表和查询索引。

## 启动

开发或局域网测试：

```bash
php -S 0.0.0.0:8080 index.php
```

浏览器访问 `http://服务器IP:8080/` 即可打开响应式监控页面。页面提供：

- 每 10 秒分别轮询最新温度和湿度
- 24 小时、7 天、30 天温湿度双曲线
- 按起止时间查询历史记录
- 手机端自适应布局

访问 `http://服务器IP:8080/analytics.html` 可打开数据分析页，查看温湿度散点分布、
分时波动区间和 24 小时分布；散点图支持逐点、逐天和画线播放。也可以使用
`index.php?action=analytics`。

生产环境可直接将此目录放入 Nginx/Apache 网站目录，并将 `index.php` 设为默认首页。所有接口都使用查询参数路由，不需要配置伪静态或 URL Rewrite。

## 配置采集器

在 Python 项目的 `config.json` 中填写：

```json
{
  "webhook": {
    "url": "http://服务器IP:8080/index.php?action=webhook",
    "timeout": 10,
    "headers": {}
  }
}
```

服务收到的 JSON 格式：

```json
{
  "timestamp": "2026-08-16T14:00:58+00:00",
  "address": "A4:C1:38:12:34:56",
  "model": "MJWSD06MMC",
  "product_id": "0x55B5",
  "temperature": 26.4,
  "humidity": 74,
  "battery": null,
  "rssi": -51
}
```

成功响应：

```json
{"ok":true,"id":1}
```

## Webhook 写入鉴权

修改 `index.php` 顶部的 token：

```php
$webhookToken = 'replace-with-a-random-secret';
```

采集器配置相同 token：

```json
{
  "webhook": {
    "url": "http://服务器IP:8080/index.php?action=webhook",
    "headers": {
      "Authorization": "Bearer replace-with-a-random-secret"
    }
  }
}
```

该 token 只用于 `POST index.php?action=webhook` 写入鉴权，所有 `GET` 查询接口均不需要鉴权。将 `$webhookToken` 设为空字符串可关闭写入鉴权。

## API

### 健康检查

```http
GET /index.php?action=health
```

```json
{"status":"ok"}
```

### 查询历史数据

```http
GET /index.php?action=readings
```

支持的查询参数：

- `limit`：返回条数，默认 `100`，最大 `25000`
- `address`：按设备 MAC 过滤
- `from`：ISO 8601 起始时间
- `to`：ISO 8601 结束时间

示例：

```bash
curl "http://127.0.0.1:8080/index.php?action=readings&address=A4:C1:38:12:34:56&limit=20"
```

响应：

```json
{
  "items": [
    {
      "id": 1,
      "timestamp": "2026-08-16T14:00:58.000000Z",
      "address": "A4:C1:38:12:34:56",
      "model": "MJWSD06MMC",
      "product_id": "0x55B5",
      "temperature": 26.4,
      "humidity": 74,
      "battery": null,
      "rssi": -51,
      "received_at": "2026-08-16T14:01:00.000000Z"
    }
  ],
  "count": 1
}
```

### 查询最新数据

全部设备中的最新一条：

```http
GET /index.php?action=latest
```

指定设备：

```bash
curl "http://127.0.0.1:8080/index.php?action=latest&address=A4:C1:38:12:34:56"
```

只查找最近一条包含温度或湿度的数据：

```http
GET /index.php?action=latest&field=temperature
GET /index.php?action=latest&field=humidity
```

## 手动测试 Webhook

```bash
curl -X POST "http://127.0.0.1:8080/index.php?action=webhook" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp":"2026-08-16T14:00:58+00:00",
    "address":"A4:C1:38:12:34:56",
    "model":"MJWSD06MMC",
    "product_id":"0x55B5",
    "temperature":26.4,
    "humidity":74,
    "battery":null,
    "rssi":-51
  }'
```

## 行为说明

### 平均值与有效时长

- 分析页的总体均值、分时均值、24 小时分布，以及日历的每日均值，统一使用时间加权：`平均值 = Σ(读数 × 有效毫秒数) / Σ有效毫秒数`。
- 每台设备、每项指标独立计算。非空读数从记录时间起保持到下一条同项有效读数，最长保持 2 小时；`null` 不作为 0，也不会延长该指标的有效期。
- 超过有效期的时段、第一条有效读数之前的时段均为未知，不计入平均值分母。没有有效时长时显示 `--`，不会退回按条数平均。末条读数只延续到有效期或本次查询结束时间，取较早者。
- 查询额外包含起点之前 2 小时的记录，用于确定窗口起始状态；跨小时、跨日的持续时间在边界处分割。小时和日期沿用浏览器本地时区，当天、当月只统计到本次请求开始时。
- 多设备的平均值按有效设备时长合并；有效覆盖率为有效设备时长除以查询窗口时长与本次查询设备数的乘积。它不代表在线率，没有心跳时无法区分数值稳定和设备失联。
- 当前记录包含采集器缓存的温湿度值，尚无每项指标的原始采样时间。因此有效期按该项非空记录的时间计算，而非硬件的实际采样时间。
- 达到 25,000 条查询上限时，页面提示数据可能不完整；统计只依据返回数据，缺少的时段不会补成 0。散点图的 Pearson r 仍为成对记录的样本相关系数。

部署时需同时更新 `dashboard.html`、`analytics.html`、`time-weighted.js` 和 `index.php`。共享统计脚本通过 `index.php?action=statistics-script` 提供，无需新增数据库字段。

### 接口行为

- 时间戳统一转换为 UTC 后存入 MySQL
- 请求体最大 64 KiB
- 温度和湿度至少有一个非 `null`
- webhook 写入失败返回 JSON 错误和对应 HTTP 状态码
- 查询结果按 `id` 从新到旧排列
