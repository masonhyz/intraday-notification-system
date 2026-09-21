"""Who gets told.

Routing is deliberately indirect. A rule names an *audience* - a role, the
queue's owners, or "the agent involved" - rather than a list of people, so one
rule covers a whole team and keeps working as people join and leave. The
"notify the agent involved" target is what makes a single adherence rule serve
800 agents instead of asking 800 agents to configure one.
"""

from __future__ import annotations

from .models import AudienceType, Rule, Subject, SubjectType, User
from .state import AgentState, EntityState
from .store import UserRepo


def resolve_audience(
    rule: Rule,
    subject: Subject,
    state: EntityState | None,
    users: UserRepo,
) -> list[User]:
    """Expand a rule's audience into concrete recipients, de-duplicated.

    Order is stable (first mention wins) so notification output is deterministic
    and testable.
    """
    resolved: dict[str, User] = {}

    for target in rule.audience:
        for user in _resolve_target(target, subject, state, users):
            resolved.setdefault(user.id, user)

    return list(resolved.values())


def _resolve_target(target, subject: Subject, state, users: UserRepo) -> list[User]:
    if target.type is AudienceType.USER:
        user = users.get(target.value or "")
        return [user] if user else []

    if target.type is AudienceType.ROLE:
        from .models import Role

        try:
            return users.by_role(Role(target.value))
        except ValueError:  # pragma: no cover - guarded by rule validation
            return []

    if target.type is AudienceType.SUBJECT_AGENT:
        if subject.type is not SubjectType.AGENT:
            return []
        user = users.by_agent_id(subject.id)
        # An agent with no user account simply has nobody to notify; the rest of
        # the audience still gets the message.
        return [user] if user else []

    if target.type is AudienceType.QUEUE_LEADS:
        return users.responsible_for_queues(affected_queues(subject, state))

    return []  # pragma: no cover - AudienceType is exhaustive above


def affected_queues(subject: Subject, state: EntityState | None) -> list[str]:
    """Which queues a subject rolls up to, for queue-owner routing."""
    if subject.type is SubjectType.QUEUE:
        return [subject.id]
    if isinstance(state, AgentState):
        return list(state.queue_ids)
    return []
