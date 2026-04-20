# 热门币种候选池

一个零构建的小应用：

- 每天 `08:00`（`Asia/Shanghai`）自动抓取 Binance USDT 永续过去 24 小时涨幅最高的前 10 个币种
- 将这些币种加入候选池
- 每个币种至少保留 10 天；之后按天检查，若当天 `high` 没有突破前 7 天高点，则记 1 次失败，连续 3 次失败后移除
- 首次启动会自动回补过去 20 天到当前时刻的数据
- 页面提供候选池列表和“入池后首点归一化 = 100”的对比曲线

## 启动

先安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

再启动：

```bash
python3 /Users/ww/Documents/top-markets/server.py
```

默认地址：

```text
http://127.0.0.1:8008
```

## 说明

- 市场数据获取逻辑已内置在本项目中，不再依赖外部 `quantool` 代码
- 历史 K 线通过 Binance Futures REST API 直接获取，并缓存到本地
- 状态文件保存在 `storage/state.json`
- K 线缓存保存在 `data/market_cache`
- 点击页面上的“立即更新一次”会按当前时刻手动跑一轮
- 也可以手动回补：

```bash
curl -X POST "http://127.0.0.1:8008/api/backfill?days=20"
```
