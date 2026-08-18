# 双指标月线云端数据

- 云端只保存公开行情结果，不保存个人资料。
- `EM_API_KEY` 必须配置为仓库加密 Secret，不得写入代码、JSON或APK。
- 定时任务在A股交易时段约每30分钟运行一次，手机只读取 `public/data.json`。
- 手机必须校验 `schemaVersion`、`generatedAt`、`latestTradeDate` 和两个指标是否完整；异常数据不得覆盖上次已核实数据。
- 固定数据地址：`https://raw.githubusercontent.com/wang283162973/dual-market-monthly-data/main/public/data.json`
- 已完成交易日使用妙想正式日线；只有公开实时行情日期晚于正式日线时，才追加当天盘中临时值。
