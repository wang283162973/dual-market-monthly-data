# 十五项月线云端数据

- 手机固定读取 `public/data.json`，历史月K随APK内置，联网后只接受完整的15项新版数据。
- `state/current_month.json` 保存当月逐日原始行情；更新器先追加当天数据，再重算当月开、高、低、收，不能用当天快照冒充整月数据。
- 全A三项使用东方财富妙想“全部A股”正式日线；黄金、有色、券商和互联网金融使用东方财富股票快照；4只个股使用东方财富前复权日线。
- 云端交易日约每30分钟更新。15项能够共同确认的最晚日期写入 `latestTradeDate`。
- 任一接口失败时保留上次正确值，并把失败原因写入 `quality.failures`，不得用空值覆盖历史。
- `EM_API_KEY` 只存放在GitHub加密Secret中，不能写入代码、JSON或APK。
- 固定数据地址：`https://raw.githubusercontent.com/wang283162973/dual-market-monthly-data/main/public/data.json`
