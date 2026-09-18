"""预算闸与运行对账：跑之前拦一下，跑完之后对一次账。

**为什么要有这一层**：整图运行是「一次点击派出几十次调用」的入口——一条链上七八个节点，
分镜图一节点就是十几张图，逐镜出片又是十几段视频。点之前没有人说得清这一下要花多少次，
点之后也没有人说得清刚才到底发了多少次。前者靠预估，后者靠 run_id 把这一跑串起来。

**为什么闸门是「再确认一次」而不是直接拒绝**：这个项目里没有任何东西能算出金额——
用户用的是自己的 key，各家的价目表不一样，我们也没有价格表。所以这里只管**可数的东西**
（调用次数），并且在超限时要求一个明确的确认动作，而不是替用户决定「不给你跑」。
零成本的东西（本机 ComfyUI 工作流）不计入。

**为什么不采集 token / 费用用量**：那要动四个适配器与十几处调用方，而多数供应商在
生图接口上根本不回 usage；回了也换不成钱（没有价格表）。这一版只把**我们自己发出去的账**
记准——派了多少次、成了几条、失败几条、出了多少产物。要算钱，拿这些数字乘自己的单价即可。
"""

from __future__ import annotations

# 「整图运行调用上限」的配置键；0 = 不限制
BUDGET_KEY = "safety.run_call_budget"
DEFAULT_BUDGET = 0

# 对账时调用数差多少才值得单独说一句（差 1～2 次属于正常波动）
NOTABLE_DELTA = 3


def call_budget() -> int:
    """读配置里的上限。读不到或值不合理时当作「不限制」，绝不因为配置坏了就拦住用户。"""
    from app.services.config_center_service import runtime_value

    try:
        value = int(runtime_value(BUDGET_KEY, DEFAULT_BUDGET) or 0)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET
    return max(0, value)


def gate(calls: int, pending: int, *, limit: int) -> dict:
    """算出闸门状态，交给确认弹窗决定要不要「再确认一次」。

    - `exceeds`：**已经确定**超过上限（已知调用数就超了）→ 必须勾选后才能运行；
    - `uncertain`：还没超，但有节点的次数要等上游跑完才知道 → 只能提示「可能会更多」。
      不能把这种当成超限去拦：那样一个还没跑过的项目会永远点不动「运行整图」。
    """
    calls = max(0, int(calls))
    pending = max(0, int(pending))
    limit = max(0, int(limit))
    exceeds = bool(limit) and calls > limit
    uncertain = bool(limit) and not exceeds and pending > 0
    return {
        "limit": limit,
        "calls": calls,
        "pendingNodes": pending,
        "exceeds": exceeds,
        "uncertain": uncertain,
        # 客户端要回传的确认值：接口拿它和**服务端此刻重算的**调用数比，
        # 所以「预览之后又改了画布」这种情况挡得住（那时预估值已经过时了）
        "ack": calls if exceeds else 0,
    }


def gate_message(state: dict) -> str:
    """给弹窗/报错用的一句话。超限与「还不确定」说法必须不一样，否则是在吓人。"""
    limit = state.get("limit") or 0
    calls = state.get("calls") or 0
    if state.get("exceeds"):
        return (
            f"这一跑已知就要调用 {calls} 次，超过你设的上限 {limit} 次"
            f"（「系统设置 → 安全」里可调）。确认要跑就勾选下面的框。"
        )
    if state.get("uncertain"):
        return (
            f"已知调用 {calls} 次，上限 {limit} 次；还有 {state.get('pendingNodes')} 个节点的次数"
            "要等上游跑完才知道，实际可能更多。"
        )
    return ""


def verdict(estimate: int, actual: int, *, failed: int = 0) -> dict:
    """跑完之后的一句话对账结论。

    三种情况说法不同，**不能都写成「完成」**：实际比预估多，通常意味着有东西被重试或者
    上游切了更多块；比预估少，通常是失败或上游截断。这两种都需要用户看一眼。
    """
    estimate = max(0, int(estimate))
    actual = max(0, int(actual))
    failed = max(0, int(failed))
    delta = actual - estimate
    if delta == 0:
        level, text = "ok", f"和预估一致：这一跑实际调用 {actual} 次。"
    elif delta > 0:
        level = "warn" if delta >= NOTABLE_DELTA else "ok"
        text = f"比预估多 {delta} 次：预估 {estimate} 次，实际 {actual} 次。"
        if level == "warn":
            text += "多半是中途有任务被重试，或者上游把内容切成了更多块。"
    else:
        level = "warn" if -delta >= NOTABLE_DELTA else "ok"
        text = f"比预估少 {-delta} 次：预估 {estimate} 次，实际 {actual} 次。"
        if level == "warn":
            text += "看一下是不是有节点因为上游没产物被整段跳过了。"
    if failed:
        text += f"其中 {failed} 条失败——它们不会因为失败就不计费，去任务中心看看原因。"
    return {"level": level, "text": text, "delta": delta, "estimate": estimate, "actual": actual}
