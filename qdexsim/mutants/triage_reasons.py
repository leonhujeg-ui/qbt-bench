"""漏网突变体的逐条判定：id → (kind, 理由)。kind ∈ equivalent（等价：行为不变）/ fixed（已补判据，重跑 --ids 已抓住）/ pending（待补）。
由人写，triage.py 读；--check 要求每个漏网都有判定且没有 pending。"""

REASONS = {
}
