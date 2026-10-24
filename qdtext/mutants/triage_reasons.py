"""漏网突变体的逐条判定：{突变 id: ("equivalent" | "fixed", 说明)}。
equivalent = 等价突变（行为不变或只改了不可观测的常量，任何测试都抓不到）；fixed = 首轮漏网、已补判据，重跑后应被抓住
（run.py --ids 重跑并入结果，记录里 first_run=survived）。
triage.py 把这里的判定与 results/mutation.json 对拍：漏网却没判定 → 报错；判了 fixed 却仍漏网 → 报错。"""
REASONS = {
    # ---- 等价 / 不可观测常量 ----
    "cdp:1395:const:65537": ("equivalent", "socket.recv 的缓冲区大小 65536→65537，只影响一次读多少字节，不影响任何输出"),
    "cdp:198:delete_stmt": ("equivalent", "等 DevToolsActivePort 的轮询里去掉 sleep(0.05)：变成忙等，结果相同（性能差异测试不该钉）"),
    "cdp:399:const:45.0": ("equivalent", "Browser 构造函数的默认 timeout 30→45 秒：所有调用都在远小于 30 秒内完成，只有卡死时才可观测"),
    "cdp:866:const:False": ("equivalent", "导航后「等两帧让排版落定」的 Runtime.evaluate 不 awaitPromise：只影响时序，排版结果在无头 Chromium 上一致"),
    "common:279:const:3": ("equivalent", "SEVERITY_ORDER 里 info 的序号 2→3：仍然排在 yellow(1) 之后，排序结果不变"),
    "common:1382:const:0": ("equivalent", "write_json 的 indent 1→0：只改 JSON 缩进，读回来内容相同"),
    "common:1382:const:2": ("equivalent", "write_json 的 indent 1→2：同上"),
    "locale_lint:1773:const:3": ("equivalent", "over 字段 round(…, 2)→round(…, 3)：字宽都是 0.5 的倍数，两位小数已精确，值不变"),
    "mobile_layout:1161:const:-1": ("equivalent", "argparse 的 description 取模块 docstring 第 0 行→最后一行：只影响 --help 文本"),
    "mobile_layout:1231:const:1": ("equivalent", "meta.seconds 的 round(…, 2)→round(…, 1)：耗时统计的小数位，不是判定"),
    "wordy:637:const:-1": ("equivalent", "resolve_key 里 hits[0]→hits[-1]：前一行已保证 len(hits) == 1，两者同一个元素"),
    "wordy:1466:const:1": ("equivalent", "argparse 的 description 取 docstring 第 0 行→第 1 行：只影响 --help 文本"),
}
