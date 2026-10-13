# j-j-j-j 回测用的合约 K 线

- 来源：豆包分享的合约 K 线包（k/*.parquet，原包约 7.8GB），只留了 252 个币：NFI 头部币名单里的币，或者有 2 年以上历史的币。
- 周期：5 分钟。时间最早 2020-01-01，最晚 2026-09-30（各币上市时间不同）。
- 每个文件一个币，列：`ts`（毫秒时间戳，UTC，K 线开始时间）、`open`、`high`、`low`、`close`、`volume`、`quote_volume`、`taker_buy_volume`、`taker_buy_quote_volume`。
- 读取：`pandas.read_parquet("k/BTC.parquet")`。
