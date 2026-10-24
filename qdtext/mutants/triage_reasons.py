"""漏网突变体的逐条判定：{突变 id: ("equivalent" | "fixed", 说明)}。
equivalent = 等价突变（行为不变，任何测试都抓不到）；fixed = 已补判据，重跑后应被抓住（run.py --ids 重跑并入结果）。
triage.py 把这里的判定与 results/mutation.json 对拍：漏网却没判定 → 报错；判了 fixed 却仍漏网 → 报错。"""
REASONS = {
}
