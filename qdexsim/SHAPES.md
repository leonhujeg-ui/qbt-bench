# SHAPES — 五家方言报文形状的可信度清单

**读法**：本次开发环境里五家官方文档站全部不可达（`developers.binance.com`、`bybit-exchange.github.io`、`www.okx.com`、
`www.bitget.com`、`openapidoc.bitunix.com` 均 DNS 解析失败，2026-10-23 实测），**所有形状都凭记忆写**，没有一条经过文档核对。
下面逐接口列出字段，并按三档标可信度：

- **稿**：记得很清楚、多次用过的字段（路径、必填参数、主要返回字段）。仍然没核对过，搬运前请对照文档。
- **待核**：不确定的字段名 / 取值 / 错误码 / HTTP 状态——写进代码是为了让仿真器能跑，**不要把它当文档用**。
- **自定**：真实接口不存在或我不知道有没有，qdexsim 自己加的（只为让判据套件能跑）。

代码里每个 handler 的 docstring 写了对应的官方文档章节名；行尾 `# 待核` 注释与本表一致。规范模型（engine）与方言字段的翻译在
`dialects/<name>.py`，客户端侧翻译在 `adapters.py`。

---

## 0. 五家通用的简化（已知与真实不同）

| 项 | qdexsim 的做法 | 真实情况 |
|---|---|---|
| 持仓模式 | 只有单向净持仓（Binance BOTH / Bybit positionIdx 0 / OKX net / Bitget one_way_mode / Bitunix ONE_WAY） | 各家都有双向持仓模式，没仿 |
| 订单类型 | 只有 MARKET / LIMIT（GTC），reduceOnly | 止损 / 止盈 / 条件单 / IOC / FOK / PostOnly 都没仿 |
| K 线 | 由 tick 聚合，成交量 = 本账户成交量；各家列顺序按文档 | 真实成交量 / 买卖量字段全是 0 或占位 |
| 行情 | 只有最新价（bid = ask = last），没有盘口 | 深度 / 24h 统计都是占位 0 |
| 限频 | 只按概率注入，没有权重 / 桶模型 | 各家都有按权重或按接口的限频表 |
| 鉴权细节 | 签名算法 / 头 / 时间窗按记忆实现（见各节） | IP 白名单、权限位、子账户都没仿 |
| 错误码 | 每种规范错误映射一个码，同码多义时客户端按 `adapters.KIND_PRIORITY` 反查 | 真实错误码远多于此 |

---

## 1. Binance USDⓈ-M Futures（`dialects/binance.py`，前缀 `/fapi/`）

鉴权：头 `X-MBX-APIKEY`；参数 `timestamp`、`recvWindow`（缺省 5000）、`signature` = HMAC-SHA256(secret, 查询串 + 请求体)（**稿**）。
时间窗 `timestamp < serverTime + 1000 且 serverTime − timestamp ≤ recvWindow`（**稿**）。签名先验还是时间窗先验：**待核**（本仿真先验签名）。

| 接口 | 路径 | 字段 | 可信度 |
|---|---|---|---|
| 服务器时间 | GET /fapi/v1/time | `serverTime`(int ms) | 稿 |
| 交易对 | GET /fapi/v1/exchangeInfo | `symbols[].{symbol,pair,contractType,status,baseAsset,quoteAsset,marginAsset,pricePrecision,quantityPrecision,filters[]}`，filters 含 PRICE_FILTER(tickSize) / LOT_SIZE(stepSize,minQty) / MIN_NOTIONAL(notional) | 稿；`status` 非交易时的取值 "BREAK" **待核**；`rateLimits`/`assets` 占位 |
| 最新价 | GET /fapi/v1/ticker/price | `{symbol,price,time}`，不带 symbol 返回列表 | 稿 |
| K 线 | GET /fapi/v1/klines | 12 列 `[openTime,o,h,l,c,vol,closeTime,quoteVol,trades,takerBuyBase,takerBuyQuote,ignore]`，interval 1m/3m/5m/15m/30m/1h/4h/1d | 稿（只实现了这 8 个 interval） |
| 账户 | GET /fapi/v2/account | `totalWalletBalance,totalUnrealizedProfit,totalMarginBalance,availableBalance,assets[],positions[]` | 稿；v3 已存在、字段差异 **待核** |
| 持仓 | GET /fapi/v2/positionRisk | `positionAmt,entryPrice,markPrice,unRealizedProfit,liquidationPrice,leverage,marginType,positionSide,notional,updateTime` | 稿；`breakEvenPrice`、`isAutoAddMargin` **待核** |
| 下单 | POST /fapi/v1/order | 入参 `symbol,side,type,quantity,price,timeInForce,newClientOrderId,reduceOnly`；返回 `orderId,clientOrderId,status,price,avgPrice,origQty,executedQty,cumQuote,timeInForce,type,reduceOnly,side,positionSide,updateTime` | 稿；无 cid 时自动 `auto<id>`：**自定**（真实会生成随机串） |
| 撤单 | DELETE /fapi/v1/order | `orderId` 或 `origClientOrderId`；返回同下单形状 status CANCELED | 稿 |
| 查单 | GET /fapi/v1/order | 同上 | 稿 |
| 未成交 | GET /fapi/v1/openOrders | 列表 | 稿 |
| 成交 | GET /fapi/v1/userTrades | `buyer,commission,commissionAsset,id,maker,orderId,price,qty,quoteQty,realizedPnl,side,positionSide,symbol,time` | 稿 |
| 杠杆 | POST /fapi/v1/leverage | 返回 `{leverage,maxNotionalValue,symbol}` | 稿 |
| 保证金模式 | POST /fapi/v1/marginType | `marginType=ISOLATED|CROSSED`；成功 `{code:200,msg:"success"}`；不变 -4046 | 稿 |

错误码（HTTP, code）：-1022 签名(400) **稿**；-1021 时间窗(400) **稿**；-2015 密钥(401) **稿**；-1102 缺参(400) **稿**；-1121 Invalid symbol **稿**；
-1111 精度(400) **稿**（qty 与 price 同码）；-4164 最小名义 **稿**；-2013 查单不存在 **稿**；-2011 撤单 Unknown order sent **稿**；-2019 保证金不足 **稿**；
-2022 ReduceOnly rejected **稿**；-4028 杠杆 **待核**；-4046/-4048 保证金模式 **稿**；-1003 限频(429 + Retry-After) **稿**；-1001 内部错(500) **稿**；
**-4116 重复 newClientOrderId：待核**（也可能是别的码）；-2010 "Symbol is not trading"（暂停交易）：**待核**；-1016 维护(503)：**待核**。

## 2. Bybit v5 linear（`dialects/bybit.py`，前缀 `/v5/`）

鉴权：头 `X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-SIGN / X-BAPI-RECV-WINDOW`，sign = HMAC-SHA256(secret, ts + key + recv + (GET 查询串 | POST 原始体))（**稿**）。
时间窗 `server − recv ≤ ts < server + 1000`（**稿**）。信封 `{retCode,retMsg,result,retExtInfo,time}`（**稿**）。
**鉴权错的 HTTP 状态：待核**（本仿真全部 HTTP 200 + retCode）。

| 接口 | 路径 | 字段 | 可信度 |
|---|---|---|---|
| 服务器时间 | GET /v5/market/time | `result.{timeSecond,timeNano}`（字符串） | 稿 |
| 交易对 | GET /v5/market/instruments-info?category=linear | `list[].{symbol,contractType,status,baseCoin,quoteCoin,settleCoin,priceScale,leverageFilter{},priceFilter{tickSize},lotSizeFilter{qtyStep,minOrderQty,minNotionalValue},fundingInterval}` | 稿；`status` 非交易值 "Closed" **待核** |
| 行情 | GET /v5/market/tickers | `list[].{symbol,lastPrice,markPrice,indexPrice,bid1Price,ask1Price,fundingRate,nextFundingTime,...}` | 稿 |
| K 线 | GET /v5/market/kline | `list[][start,o,h,l,c,volume,turnover]`（字符串，最新在前），interval 1/3/5/15/30/60/120/240/D | 稿 |
| 钱包 | GET /v5/account/wallet-balance?accountType=UNIFIED | `list[0].{accountType,totalEquity,totalWalletBalance,totalAvailableBalance,coin[].{coin,equity,walletBalance,...}}` | 稿 |
| 持仓 | GET /v5/position/list?category=linear | `list[].{positionIdx,symbol,side(Buy/Sell/""),size,avgPrice,positionValue,tradeMode,leverage,markPrice,liqPrice,positionIM,unrealisedPnl,cumRealisedPnl,createdTime,updatedTime}` | 稿；空仓 `side` 取值 "" **待核**；真实接口要求 symbol 或 settleCoin，本仿真可省 |
| 下单 | POST /v5/order/create | 入参 `category,symbol,side,orderType,qty,price,timeInForce,orderLinkId,reduceOnly`；返回 `result.{orderId,orderLinkId}` | 稿 |
| 撤单 | POST /v5/order/cancel | `orderId` 或 `orderLinkId`；返回 `{orderId,orderLinkId}` | 稿 |
| 查单 / 未成交 | GET /v5/order/realtime | 带 orderId/orderLinkId 查单，不带列未成交；订单字段 `orderId,orderLinkId,symbol,price,qty,side,orderType,orderStatus,cumExecQty,cumExecValue,cumExecFee,avgPrice,leavesQty,timeInForce,reduceOnly,createdTime,updatedTime` | 稿；**查不到的单返回空 list（retCode 0）还是 110001：待核**（本仿真返回空 list） |
| 成交 | GET /v5/execution/list | `list[].{symbol,orderId,orderLinkId,side,orderPrice,orderQty,leavesQty,orderType,execFee,execId,execPrice,execQty,execType,execValue,execTime,isMaker,feeRate}` | 稿 |
| 杠杆 | POST /v5/position/set-leverage | `buyLeverage == sellLeverage`；不变 → 110043 | 稿 |
| 保证金模式 | POST /v5/position/switch-isolated | `tradeMode 0/1`；不变 → 110026 | 稿 |

错误码：10004 签名 **稿**（retMsg 里 origin_string 的格式 **待核**）；10002 时间窗 **稿**；10003 密钥 **稿**；10001 参数错 **稿**（symbol / qty / price / 暂停交易全用它：**待核**）；
110072 重复 orderLinkId **稿**；110001 单不存在 **稿**；110007 余额不足 **稿**；110017 reduce-only **稿**；110043 / 110026 不变 **稿**；110094 最小名义 **待核**；110013 杠杆 **待核**；
10006 限频 **稿**，头 `X-Bapi-Limit-Status / X-Bapi-Limit / X-Bapi-Limit-Reset-Timestamp` **稿**，**HTTP 状态 200 vs 429：待核**（本仿真 200）；10016 服务器错 **稿**；维护中的码 **待核**。

## 3. OKX v5 SWAP（`dialects/okx.py`，前缀 `/api/v5/`）

鉴权：头 `OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP(ISO 8601 毫秒 Z) / OK-ACCESS-PASSPHRASE`，sign = base64(HMAC-SHA256(secret, ts + method + requestPath(含查询串) + body))（**稿**）。
时间窗 30 秒（**稿**）。信封 `{code,msg,data}`，鉴权错 HTTP 401（**稿**）。张数：`sz` 为张，`ctVal` BTC 0.01 / ETH 0.1（**稿**）。

| 接口 | 路径 | 字段 | 可信度 |
|---|---|---|---|
| 时间 | GET /api/v5/public/time | `data[0].ts` | 稿 |
| 交易对 | GET /api/v5/public/instruments?instType=SWAP | `instId,uly,instFamily,settleCcy,ctVal,ctMult,ctValCcy,ctType,state,tickSz,lotSz,minSz,lever,listTime` | 稿；`state` 暂停值 "suspend" **待核** |
| 行情 | GET /api/v5/market/ticker | `last,lastSz,askPx,askSz,bidPx,bidSz,open24h,high24h,low24h,volCcy24h,vol24h,ts,sodUtc0,sodUtc8` | 稿 |
| K 线 | GET /api/v5/market/candles | `[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]` 最新在前，bar 1m/3m/5m/15m/30m/1H/2H/4H/1D | 稿 |
| 余额 | GET /api/v5/account/balance | `totalEq,adjEq,availEq,ordFroz,imr,mmr,details[].{ccy,eq,cashBal,availBal,availEq,frozenBal,ordFrozen,upl,uTime}` | 稿 |
| 持仓 | GET /api/v5/account/positions?instType=SWAP | `instId,mgnMode,posId,posSide,pos,availPos,avgPx,upl,lever,liqPx,markPx,margin,mmr,notionalUsd,cTime,uTime`，只返回非零 | 稿 |
| 下单 | POST /api/v5/trade/order | 入参 `instId,tdMode,side,ordType,sz,px,clOrdId,reduceOnly`；成功 `{code:"0",data:[{clOrdId,ordId,tag,sCode:"0",sMsg}],inTime,outTime}`；失败 `code:"1"` + `data[0].sCode` | 稿 |
| 撤单 | POST /api/v5/trade/cancel-order | `ordId` 或 `clOrdId`；`data[0].{clOrdId,ordId,sCode,sMsg}` | 稿 |
| 查单 | GET /api/v5/trade/order | `ordId,clOrdId,px,sz,ordType,side,posSide,tdMode,accFillSz,fillPx,fillSz,avgPx,state,lever,fee,feeCcy,pnl,reduceOnly("true"/"false"),cTime,uTime` | 稿 |
| 未成交 | GET /api/v5/trade/orders-pending?instType=SWAP | 列表 | 稿 |
| 成交 | GET /api/v5/trade/fills?instType=SWAP | `tradeId,ordId,clOrdId,billId,fillPx,fillSz,side,posSide,execType(T/M),feeCcy,fee(负数),ts,fillTime,fillPnl` | 稿 |
| 杠杆 | POST /api/v5/account/set-leverage | `{instId,lever,mgnMode}` → `data[0].{lever,mgnMode,instId,posSide}` | 稿 |
| 保证金模式 | POST /api/v5/account/set-position-mode | **自定映射**：OKX 没有“按品种切换保证金模式”接口，模式随 set-leverage 的 mgnMode / 下单 tdMode；这里只接受 `posMode=net_mode` | 自定 |

tdMode 与持仓模式不一致 → 本仿真拒绝 51000（**自定**：真实 OKX 的行为待核）。
错误码：50113 签名(401) **稿**；50102 时间戳(401) **稿**；50111 密钥 **稿**；50105 passphrase **待核**；50103 缺头 **待核**（缺不同头码不同）；
51000 参数 **稿**（sz / px / reduceOnly / lever / 暂停全用它：**待核**）；51001 instId 不存在 **稿**；51008 余额不足 **稿**；51020 最小数量 **稿**；
**51016 clOrdId 重复：待核**；51603 查单不存在 **稿**；51400 撤单失败不存在 **待核**（51401/51402 分已撤 / 已成）；50011 限频(429) **稿**，重试头 **待核**（文档未见）；
50026 系统错 **待核**；50001 维护(503) **稿**。

## 4. Bitget v2 mix USDT-FUTURES（`dialects/bitget.py`，前缀 `/api/v2/`）

鉴权：头 `ACCESS-KEY / ACCESS-SIGN / ACCESS-TIMESTAMP(ms) / ACCESS-PASSPHRASE`，sign = base64(HMAC-SHA256(secret, ts + METHOD + path + ("?"+query) + body))（**稿**）。
时间窗 30 秒（**待核**）。信封 `{code:"00000",msg:"success",requestTime,data}`，错误 HTTP 400 + code 字符串（**稿**）。

| 接口 | 路径 | 字段 | 可信度 |
|---|---|---|---|
| 时间 | GET /api/v2/public/time | `data.serverTime`(字符串) | 稿 |
| 交易对 | GET /api/v2/mix/market/contracts?productType=USDT-FUTURES | `symbol,baseCoin,quoteCoin,makerFeeRate,takerFeeRate,minTradeNum,priceEndStep,volumePlace,pricePlace,sizeMultiplier,symbolType,minTradeUSDT,symbolStatus,fundInterval,minLever,maxLever,...` | 稿；`symbolStatus` 非 normal 的值 "maintain" **待核**；其余比率类字段占位 |
| 行情 | GET /api/v2/mix/market/ticker | `data[0].{symbol,lastPr,askPr,bidPr,high24h,low24h,ts,indexPrice,fundingRate,markPrice,...}` | 稿 |
| K 线 | GET /api/v2/mix/market/candles | `[ts,o,h,l,c,baseVol,quoteVol]`，granularity 1m/3m/5m/15m/30m/1H/4H/1D | 稿；**顺序旧→新：待核** |
| 账户 | GET /api/v2/mix/account/account?symbol&productType&marginCoin | `marginCoin,locked,available,crossedMaxAvailable,isolatedMaxAvailable,maxTransferOut,accountEquity,usdtEquity,crossedMarginLeverage,isolatedLongLever,isolatedShortLever,marginMode,posMode,unrealizedPL,...` | 稿 |
| 持仓 | GET /api/v2/mix/position/all-position?productType&marginCoin | `marginCoin,symbol,holdSide,marginSize,available,locked,total,leverage,achievedProfits,openPriceAvg,marginMode,posMode,unrealizedPL,liquidationPrice,keepMarginRate,markPrice,breakEvenPrice,cTime,uTime` | 稿 |
| 下单 | POST /api/v2/mix/order/place-order | `symbol,productType,marginMode(crossed/isolated),marginCoin,size,price,side,orderType,force,clientOid,reduceOnly(YES/NO)` → `{clientOid,orderId}` | 稿 |
| 撤单 | POST /api/v2/mix/order/cancel-order | `orderId` 或 `clientOid` → `{clientOid,orderId}` | 稿 |
| 查单 | GET /api/v2/mix/order/detail | `symbol,size,orderId,clientOid,baseVolume,fee,price,priceAvg,state,side,force,totalProfits,posSide,marginCoin,quoteVolume,orderType,leverage,marginMode,reduceOnly,posMode,cTime,uTime` | 稿 |
| 未成交 | GET /api/v2/mix/order/orders-pending | `{entrustedList:[...],endId}` | 稿 |
| 成交 | GET /api/v2/mix/order/fills | `{fillList:[{tradeId,symbol,orderId,price,baseVolume,feeDetail[{feeCoin,totalFee,...}],side,quoteVolume,profit,tradeScope,cTime}],endId}` | 稿 |
| 杠杆 | POST /api/v2/mix/account/set-leverage | → `{symbol,marginCoin,longLeverage,shortLeverage,crossMarginLeverage,marginMode}` | 稿 |
| 保证金模式 | POST /api/v2/mix/account/set-margin-mode | `marginMode isolated/crossed` → `{symbol,marginCoin,longLeverage,shortLeverage,marginMode}` | 稿 |

错误码：40009 签名 **稿**；40008 时间戳 **稿**；40006 密钥 **稿**；40012 passphrase **待核**；40001 缺头 **待核**；40034 参数不存在（symbol）**稿**；
40808 参数校验异常（精度）**待核**；45110 最小名义 **稿**；**40786 重复 clientOid：稿**；40768 单不存在 **稿**；40762 余额不足 **稿**；40774 reduce-only **待核**；
40797 杠杆 **待核**；45117 保证金模式有仓 **待核**；429 限频 **待核**（无重试头）；50001 系统错 **待核**；40200 维护 **稿**；40020 暂停交易 **待核**；404 的码 40404 **待核**。

## 5. Bitunix Futures（`dialects/bitunix.py`，前缀 `/api/v1/futures/`）— 整体可信度最低

鉴权（**待核**）：头 `api-key / nonce / timestamp / sign`；digest = SHA256(nonce + timestamp + api_key + queryParams + body)，sign = SHA256(digest + secret)，
两次 SHA-256 而非 HMAC；queryParams = 键名升序、key+value 直接拼接无分隔符；时间窗 ±30 秒 **待核**；nonce 重放拒绝 **自定**。信封 `{code:0,data,msg:"Success"}`（**稿**）。

| 接口 | 路径 | 字段 | 可信度 |
|---|---|---|---|
| 时间 | GET /api/v1/futures/market/time | **自定**：Bitunix 文档未见服务器时间接口 | 自定 |
| 交易对 | GET /api/v1/futures/market/trading_pairs?symbols= | `symbol,base,quote,minTradeVolume,maxLeverage,minLeverage,makerFee,takerFee,basePrecision,quotePrecision,symbolStatus` | 待核；`tickSize/stepSize` 字段名 **自定** |
| 行情 | GET /api/v1/futures/market/tickers?symbols= | `symbol,markPrice,lastPrice,open,last,quoteVol,baseVol,high,low` | 稿 |
| K 线 | GET /api/v1/futures/market/kline | `[{open,high,low,close,quoteVol,baseVol,time,type}]` | 待核 |
| 账户 | GET /api/v1/futures/account?marginCoin=USDT | `marginCoin,available,frozen,margin,transfer,positionMode,crossUnrealizedPNL,isolationUnrealizedPNL,bonus` | 待核 |
| 持仓 | GET /api/v1/futures/position/get_pending_positions | `positionId,symbol,qty,entryValue,side,marginMode(ISOLATION/CROSS),positionMode,leverage,fee,funding,realizedPNL,margin,unrealizedPNL,liqPrice,marginRate,avgOpenPrice,ctime,mtime` | 待核 |
| 下单 | POST /api/v1/futures/trade/place_order | `symbol,qty,price,side,tradeSide,orderType,effect,clientId,reduceOnly` → `{orderId,clientId}` | 稿 |
| 撤单 | POST /api/v1/futures/trade/cancel_orders | `{symbol,orderList:[{orderId|clientId}]}` → `{successList,failureList[{orderId,clientId,errorMsg,errorCode}]}` | 待核（单笔失败是否整单报错） |
| 查单 | GET /api/v1/futures/trade/get_order_detail | `orderId,symbol,qty,tradeQty,positionMode,marginMode,leverage,price,side,orderType,effect,clientId,reduceOnly,status(INIT/NEW/PART_FILLED/CANCELED/FILLED),fee,realizedPNL,avgPrice,ctime,mtime` | 待核 |
| 未成交 | GET /api/v1/futures/trade/get_pending_orders | `{orderList,total}` | 待核 |
| 成交 | GET /api/v1/futures/trade/get_history_trades | `{tradeList[{tradeId,orderId,symbol,qty,price,side,orderType,effect,clientId,reduceOnly,fee,realizedPNL,ctime,roleType}],total}` | 待核 |
| 杠杆 | POST /api/v1/futures/account/change_leverage | `{symbol,marginCoin,leverage}` | 待核 |
| 保证金模式 | POST /api/v1/futures/account/change_margin_mode | `marginMode ISOLATION/CROSS` | 待核 |

错误码：10007 签名 **稿**；10003 api-key 缺 / 无效 **待核**；10002 参数错 **稿**（时间窗错也用它：**待核**，适配器按报文里的 "timestamp" 嗅探）；
10008 不符合规则（精度 / nonce）**待核**；20001 市场不存在 **稿**；20002 无持仓 **待核**；20003 余额不足 **稿**；20005 杠杆 **稿**；20006 有挂单不能改模式 **稿**；
20007 单不存在 **稿**；**30008 重复 clientId：自定**（不知道真实码）；30002 最小名义 **待核**；10006 限频 **待核**（HTTP 429 与重试头都是自定）；10001 网络错 **待核**；20012/20015 维护 / 暂停 **待核**。

---

## 6. 待核项汇总（搬运前必须对照文档的点）

1. 五家“重复 clientOrderId”的错误码：Binance -4116 / OKX 51016 / Bitunix 30008 待核，Bybit 110072 / Bitget 40786 较有把握。
2. 五家“暂停交易 / 下架”的交易对状态取值与下单被拒的码：全部待核。
3. Bybit 鉴权错与限频的 HTTP 状态码（200 还是 401/403/429）；Bybit realtime 查不到单是空 list 还是 110001。
4. OKX / Bitget 429 是否带重试头（本仿真不带，适配器用 1000 ms 缺省退避）。
5. Bitget 与 Bitunix 的时间窗大小；Bitunix 整个签名算法与 nonce 规则。
6. Bitunix 服务器时间接口是否存在；Bitunix 交易对精度字段名。
7. Binance 签名与时间窗哪个先验；Binance futures 账户接口 v2 → v3 的字段差异。
8. 各家 K 线列顺序与新旧方向（Binance 旧→新、Bybit / OKX 新→旧、Bitget 旧→新、Bitunix 待核）。
