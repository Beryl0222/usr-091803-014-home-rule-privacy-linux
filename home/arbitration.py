"""冲突仲裁：多个动作竞争同一设备能力时给出唯一、可解释的决定。

优先级严格按需求规定的顺序，逐级比较，全部相同再用稳定键兜底，保证可复现：

    1. 安全级别 safety_level：越大越优先（人身安全、隐私保护 > 便利）；
    2. 授权范围 authority：系统安全兜底 > 管理员 > 被授权成员 > 访客临时凭证；
    3. 时间 event_ts：越晚越优先（最新的意图覆盖较早的意图）；
    4. 稳定键 source_id / command：仅用于确定性兜底，不影响语义。

仲裁只在"同一设备 + 同一能力且命令互斥"的候选之间进行；
不互斥的动作各自保留。每个落败动作都记录具体落败原因，形成可解释链。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from home.models import RuleAction


# 授权范围（authority）的固定档位
AUTHORITY_FAILSAFE = 40   # 系统安全兜底（离线/缺传感器时的保守动作）
AUTHORITY_ADMIN = 30      # 父母等家庭管理员
AUTHORITY_MEMBER = 20     # 被显式授予该能力的成员
AUTHORITY_VISITOR = 10    # 访客临时凭证


@dataclass(frozen=True)
class Candidate:
    """进入仲裁的一个候选动作及其授权出处。"""

    action: RuleAction
    source_id: str                 # 规则 id / "visitor:<pass_id>" / "manual:<member>"
    source_name: str
    actor_id: str
    safety_level: int
    authority: int
    event_ts: float

    def key(self) -> tuple[str, str]:
        return (self.action.device_id, self.action.capability.value)


@dataclass(frozen=True)
class Suppressed:
    candidate: Candidate
    reason: str


@dataclass(frozen=True)
class Decision:
    device_id: str
    capability: str
    winner: Optional[Candidate]
    suppressed: list[Suppressed] = field(default_factory=list)

    @property
    def conflicted(self) -> bool:
        return bool(self.suppressed)

    def explain(self) -> str:
        if self.winner is None:
            return f"{self.device_id}/{self.capability}：无可用动作"
        head = (
            f"{self.device_id}/{self.capability} 执行 "
            f"「{self.winner.action.command}」<- {self.winner.source_name}"
        )
        if not self.suppressed:
            return head
        losers = "；".join(f"否决 {s.candidate.source_name}（{s.reason}）" for s in self.suppressed)
        return f"{head}；{losers}"


@dataclass(frozen=True)
class ArbitrationResult:
    decisions: list[Decision]

    @property
    def winners(self) -> list[Candidate]:
        return [d.winner for d in self.decisions if d.winner is not None]

    def for_device(self, device_id: str) -> list[Decision]:
        return [d for d in self.decisions if d.device_id == device_id]

    def explain_all(self) -> list[str]:
        return [d.explain() for d in self.decisions]


class Arbitrator:
    def arbitrate(self, candidates: list[Candidate]) -> ArbitrationResult:
        groups: dict[tuple[str, str], list[Candidate]] = {}
        for cand in candidates:
            groups.setdefault(cand.key(), []).append(cand)

        decisions: list[Decision] = []
        for (device_id, capability), group in groups.items():
            decisions.append(self._decide(device_id, capability, group))
        # 稳定输出顺序
        decisions.sort(key=lambda d: (d.device_id, d.capability))
        return ArbitrationResult(decisions)

    # ---- 内部 -----------------------------------------------------------

    def _decide(self, device_id: str, capability: str,
                group: list[Candidate]) -> Decision:
        if len(group) == 1:
            return Decision(device_id, capability, group[0])

        # 先把互斥的候选归并：命令相同的不构成竞争，可一起执行；
        # 真正竞争时，对每个命令选其最强代表，再在命令间决胜。
        by_command: dict[str, list[Candidate]] = {}
        for cand in group:
            by_command.setdefault(cand.action.command, []).append(cand)

        command_champions: list[tuple[Candidate, list[Candidate]]] = [
            (self._rank(cmd_group)[0], cmd_group) for cmd_group in by_command.values()
        ]

        if len(command_champions) == 1:
            # 命令都相同：所有候选语义一致，代表胜出，其余标记为重复
            champion, members = command_champions[0]
            suppressed = [
                Suppressed(c, "与胜出动作命令相同，无需重复执行")
                for c in members if c is not champion
            ]
            return Decision(device_id, capability, champion, suppressed)

        # 命令互斥：按排序键选出唯一冠军
        ranked = sorted(
            (champ for champ, _ in command_champions),
            key=lambda c: self._sort_key(c),
            reverse=True,
        )
        winner = ranked[0]
        suppressed: list[Suppressed] = []
        for loser in ranked[1:]:
            suppressed.append(Suppressed(loser, self._why(winner, loser)))
        return Decision(device_id, capability, winner, suppressed)

    @staticmethod
    def _sort_key(c: Candidate) -> tuple[int, int, float, str, str]:
        return (c.safety_level, c.authority, c.event_ts, c.source_id, c.action.command)

    def _rank(self, group: list[Candidate]) -> list[Candidate]:
        return sorted(group, key=self._sort_key, reverse=True)

    @staticmethod
    def _why(winner: Candidate, loser: Candidate) -> str:
        """返回落败者在三级比较中第一处被压过的原因。"""
        if loser.safety_level != winner.safety_level:
            return (
                f"安全级别 {loser.safety_level} 低于 {winner.safety_level}"
                f"（{winner.source_name}）"
            )
        if loser.authority != winner.authority:
            return (
                f"授权范围 {_authority_name(loser.authority)} 低于 "
                f"{_authority_name(winner.authority)}（{winner.source_name}）"
            )
        if loser.event_ts != winner.event_ts:
            when = "早于" if loser.event_ts < winner.event_ts else "晚于"
            return f"时间 {when} 更新的意图（{winner.source_name}）"
        return f"稳定排序兜底：{winner.source_id} 优先"


def _authority_name(authority: int) -> str:
    return {
        AUTHORITY_FAILSAFE: "系统安全兜底",
        AUTHORITY_ADMIN: "管理员",
        AUTHORITY_MEMBER: "授权成员",
        AUTHORITY_VISITOR: "访客临时凭证",
    }.get(authority, str(authority))
