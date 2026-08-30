"""Mafia game engine v3: Don / Mafia / Sheriff / Doctor / Civilian.

Phases: LOBBY -> NIGHT -> DAY -> NIGHT -> ... -> ENDED
Pure state machine, no I/O. Handlers drive phase transitions.

Night resolution order:
  1. Mafia/Don kill (Don's choice overrides Mafia's)
  2. Sheriff check (learns if target is mafia-aligned)
  3. Doctor heal (cancels death on matching target)
"""
from __future__ import annotations

import enum
import random
from dataclasses import dataclass, field
from typing import Optional


class Role(str, enum.Enum):
    DON = "don"
    MAFIA = "mafia"
    SHERIFF = "sheriff"
    DOCTOR = "doctor"
    LOVER = "lover"
    MANIAC = "maniac"
    CIVILIAN = "civilian"


MAFIA_TEAM = {Role.DON, Role.MAFIA}
TOWN_TEAM = {Role.SHERIFF, Role.DOCTOR, Role.CIVILIAN}


class Phase(str, enum.Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY = "day"
    DISCUSSION = "discussion"
    ENDED = "ended"


class Winner(str, enum.Enum):
    TOWN = "town"
    MAFIA = "mafia"
    MANIAC = "maniac"


@dataclass
class Player:
    tg_id: int
    name: str
    number: int = 0
    role: Role = Role.CIVILIAN
    alive: bool = True
    protected: bool = False
    hidden_from_checks: bool = False
    title: str = ""
    badges: str = ""


@dataclass
class RoomSettings:
    night_seconds: int = 40
    discussion_seconds: int = 60
    vote_seconds: int = 30
    min_players: int = 5
    mode: str = "classic"          # 'classic' | 'fast'
    allow_don: bool = True
    # Standard mode: only Don, Mafia, Sheriff, Doctor and Civilian.
    allow_lover: bool = False
    allow_maniac: bool = False


@dataclass
class NightActions:
    mafia_kill: Optional[int] = None
    don_kill: Optional[int] = None
    sheriff_check: Optional[int] = None
    sheriff_kill: Optional[int] = None
    doctor_heal: Optional[int] = None
    lover_block: Optional[int] = None
    maniac_kill: Optional[int] = None

    mafia_done: bool = False
    don_done: bool = False
    sheriff_done: bool = False
    doctor_done: bool = False
    lover_done: bool = False
    maniac_done: bool = False


@dataclass
class DayVotes:
    votes: dict[int, int] = field(default_factory=dict)


@dataclass
class NightResult:
    killed: list[Player] = field(default_factory=list)
    saved: list[Player] = field(default_factory=list)
    sheriff_learned: Optional[tuple[Player, bool]] = None   # (target, is_mafia)
    blocked: Optional[Player] = None


@dataclass
class DayResult:
    lynched: Optional[Player] = None


def default_role_composition(n: int, s: RoomSettings) -> list[Role]:
    """Classic composition scaled by player count."""
    if n < 4:
        raise ValueError("need at least 4 players")
    roles: list[Role] = []
    # Standard composition: Don + Mafia form the mafia team; for 5+ players
    # there are always at least two mafia-aligned roles.
    mafia_total = 1 if n <= 4 else max(2, n // 3)
    if s.allow_don and mafia_total >= 1:
        roles.append(Role.DON)
        mafia_total -= 1
    roles += [Role.MAFIA] * mafia_total
    roles.append(Role.SHERIFF)
    roles.append(Role.DOCTOR)
    while len(roles) < n:
        roles.append(Role.CIVILIAN)
    return roles[:n]


class Game:
    def __init__(self, chat_id: int, host_id: int, settings: Optional[RoomSettings] = None):
        self.chat_id = chat_id
        self.host_id = host_id
        self.settings = settings or RoomSettings()
        self.phase: Phase = Phase.LOBBY
        self.players: list[Player] = []
        self.night_actions = NightActions()
        self.day_votes = DayVotes()
        self.round: int = 0
        self.winner: Optional[Winner] = None
        self.role_preferences: dict[int, Role] = {}
        self.revived: set[int] = set()

    @property
    def MIN_PLAYERS(self) -> int:
        return self.settings.min_players

    # ---- lobby ----
    def add_player(self, tg_id: int, name: str) -> bool:
        if self.phase != Phase.LOBBY:
            return False
        if any(p.tg_id == tg_id for p in self.players):
            return False
        self.players.append(Player(tg_id=tg_id, name=name))
        return True

    def alive_players(self) -> list[Player]:
        return [p for p in self.players if p.alive]

    def by_number(self, number: int) -> Optional[Player]:
        for p in self.players:
            if p.number == number and p.alive:
                return p
        return None

    def by_tg(self, tg_id: int) -> Optional[Player]:
        for p in self.players:
            if p.tg_id == tg_id:
                return p
        return None

    def has_role(self, role: Role) -> bool:
        return any(p.role == role and p.alive for p in self.players)

    def players_of(self, role: Role) -> list[Player]:
        return [p for p in self.players if p.role == role and p.alive]

    # ---- start ----
    def start(self, rng: random.Random | None = None) -> None:
        if self.phase != Phase.LOBBY or len(self.players) < self.MIN_PLAYERS:
            raise ValueError("cannot start")
        rng = rng or random.Random()
        rng.shuffle(self.players)
        n = len(self.players)
        roles = default_role_composition(n, self.settings)
        rng.shuffle(roles)
        # Honor one-time role preference cards where possible, then fill normally.
        assigned: set[Role] = set()
        remaining_roles = roles.copy()
        for p in self.players:
            pref = self.role_preferences.get(p.tg_id)
            if pref and pref in remaining_roles:
                p.role = pref
                remaining_roles.remove(pref)
                assigned.add(pref)
        rng.shuffle(remaining_roles)
        for i, p in enumerate(self.players, start=1):
            p.number = i
            if p.role in assigned:
                continue
            p.role = remaining_roles.pop()
        self.phase = Phase.NIGHT
        self.round = 1
        self.night_actions = NightActions()

    # ---- night actions ----
    def _actor(self, tg_id: int, role: Role) -> Optional[Player]:
        p = self.by_tg(tg_id)
        if not p or not p.alive or p.role != role or self.phase != Phase.NIGHT:
            return None
        return p

    def submit_mafia_kill(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.MAFIA)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.role in MAFIA_TEAM:
            return False
        self.night_actions.mafia_kill = t.number
        self.night_actions.mafia_done = True
        return True

    def submit_don_kill(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.DON)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.role in MAFIA_TEAM:
            return False
        self.night_actions.don_kill = t.number
        self.night_actions.don_done = True
        return True

    def submit_sheriff_check(self, actor_tg: int, target: int) -> Optional[bool]:
        actor = self._actor(actor_tg, Role.SHERIFF)
        if not actor:
            return None
        t = self.by_number(target)
        if not t or t.tg_id == actor.tg_id:
            return None
        self.night_actions.sheriff_check = t.number
        self.night_actions.sheriff_done = True
        return t.role in MAFIA_TEAM

    def submit_sheriff_kill(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.SHERIFF)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.tg_id == actor.tg_id:
            return False
        self.night_actions.sheriff_kill = t.number
        self.night_actions.sheriff_done = True
        return True

    def submit_doctor_heal(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.DOCTOR)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.tg_id == actor.tg_id:
            return False
        self.night_actions.doctor_heal = t.number
        self.night_actions.doctor_done = True
        return True

    def submit_lover_block(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.LOVER)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.tg_id == actor.tg_id:
            return False
        self.night_actions.lover_block = t.number
        self.night_actions.lover_done = True
        return True

    def submit_maniac_kill(self, actor_tg: int, target: int) -> bool:
        actor = self._actor(actor_tg, Role.MANIAC)
        if not actor:
            return False
        t = self.by_number(target)
        if not t or t.tg_id == actor.tg_id:
            return False
        self.night_actions.maniac_kill = t.number
        self.night_actions.maniac_done = True
        return True

    _DONE_ATTR = {
        Role.MAFIA: "mafia_done", Role.DON: "don_done",
        Role.SHERIFF: "sheriff_done", Role.DOCTOR: "doctor_done",
        Role.LOVER: "lover_done", Role.MANIAC: "maniac_done",
    }

    def skip_night_action(self, actor_tg: int) -> bool:
        p = self.by_tg(actor_tg)
        if not p or not p.alive or self.phase != Phase.NIGHT:
            return False
        attr = self._DONE_ATTR.get(p.role)
        if not attr:
            return False
        setattr(self.night_actions, attr, True)
        return True

    def night_complete(self) -> bool:
        na = self.night_actions
        for role, attr in self._DONE_ATTR.items():
            if self.has_role(role) and not getattr(na, attr):
                return False
        return True

    def day_complete(self) -> bool:
        alive_ids = {p.tg_id for p in self.alive_players()}
        return alive_ids.issubset(self.day_votes.votes.keys())

    # ---- resolve night -> day ----
    def set_player_protected(self, tg_id: int) -> bool:
        p = self.by_tg(tg_id)
        if not p or not p.alive:
            return False
        p.protected = True
        return True

    def set_player_hidden(self, tg_id: int) -> bool:
        p = self.by_tg(tg_id)
        if not p or not p.alive:
            return False
        p.hidden_from_checks = True
        return True

    def resolve_night(self) -> NightResult:
        na = self.night_actions

        # 1. Lover block resolves first.
        blocked_tg: Optional[int] = None
        blocked_player: Optional[Player] = None
        if na.lover_block is not None:
            t = next((p for p in self.players if p.number == na.lover_block), None)
            if t and t.alive:
                blocked_tg = t.tg_id
                blocked_player = t

        def actor_blocked(role: Role) -> bool:
            if blocked_tg is None:
                return False
            actor = next((p for p in self.alive_players() if p.role == role), None)
            return actor is not None and actor.tg_id == blocked_tg

        # 2. Mafia/Don kill — Don overrides.
        mafia_target: Optional[int] = None
        if self.has_role(Role.DON) and na.don_kill is not None and not actor_blocked(Role.DON):
            mafia_target = na.don_kill
        elif na.mafia_kill is not None and self.has_role(Role.MAFIA) and not actor_blocked(Role.MAFIA):
            mafia_target = na.mafia_kill

        # 3. Maniac kill (independent).
        maniac_target: Optional[int] = (
            na.maniac_kill if na.maniac_kill is not None and not actor_blocked(Role.MANIAC) else None
        )

        # 4. Sheriff check.
        sheriff_learned: Optional[tuple[Player, bool]] = None
        if na.sheriff_check is not None and not actor_blocked(Role.SHERIFF):
            tgt = next((p for p in self.players if p.number == na.sheriff_check), None)
            if tgt:
                sheriff_learned = (tgt, tgt.role in MAFIA_TEAM and not tgt.hidden_from_checks)

        # 5. Sheriff shot (one night action: check OR shoot).
        sheriff_target: Optional[int] = (
            na.sheriff_kill if na.sheriff_kill is not None and not actor_blocked(Role.SHERIFF) else None
        )

        # 6. Doctor heal.
        heal_target = na.doctor_heal if not actor_blocked(Role.DOCTOR) else None

        # Apply deaths. Doctor protection applies to every night attack,
        # including a Sheriff's shot.
        killed: list[Player] = []
        saved: list[Player] = []
        for tnum in {mafia_target, maniac_target, sheriff_target} - {None}:
            victim = next((p for p in self.players if p.number == tnum), None)
            if not victim or not victim.alive:
                continue
            if heal_target == victim.number or victim.protected:
                saved.append(victim)
                victim.protected = False
            else:
                victim.alive = False
                killed.append(victim)

        self.phase = Phase.DISCUSSION
        self.day_votes = DayVotes()
        self._check_winner()
        return NightResult(killed=killed, saved=saved,
                           sheriff_learned=sheriff_learned, blocked=blocked_player)

    # ---- day voting ----
    def submit_vote(self, voter_tg: int, target_number: int) -> bool:
        if self.phase != Phase.DAY:
            return False
        voter = self.by_tg(voter_tg)
        if not voter or not voter.alive:
            return False
        target = self.by_number(target_number)
        if not target or target.tg_id == voter.tg_id:
            return False
        self.day_votes.votes[voter_tg] = target.number
        return True

    def submit_no_vote(self, voter_tg: int) -> bool:
        """Record an explicit abstention. Zero is reserved as the abstain marker."""
        if self.phase != Phase.DAY:
            return False
        voter = self.by_tg(voter_tg)
        if not voter or not voter.alive:
            return False
        self.day_votes.votes[voter_tg] = 0
        return True

    def resolve_day(self) -> DayResult:
        lynched: Optional[Player] = None
        if self.day_votes.votes:
            tally: dict[int, int] = {}
            for num in self.day_votes.votes.values():
                if num == 0:
                    continue
                tally[num] = tally.get(num, 0) + 1
            if not tally:
                self.phase = Phase.NIGHT
                self.round += 1
                self.night_actions = NightActions()
                self._check_winner()
                return DayResult(lynched=None)
            top = max(tally.values())
            winners = [num for num, c in tally.items() if c == top]
            if len(winners) == 1:
                lynched = next((p for p in self.players if p.number == winners[0]), None)
                if lynched:
                    lynched.alive = False
        self.phase = Phase.NIGHT
        self.round += 1
        self.night_actions = NightActions()
        self._check_winner()
        return DayResult(lynched=lynched)

    # ---- win conditions ----
    def _check_winner(self) -> None:
        alive = self.alive_players()
        mafia_alive = [p for p in alive if p.role in MAFIA_TEAM]
        maniac_alive = [p for p in alive if p.role == Role.MANIAC]
        town_alive = [p for p in alive if p.role in TOWN_TEAM]

        if not mafia_alive and not maniac_alive:
            self.winner = Winner.TOWN
            self.phase = Phase.ENDED
            return
        if not mafia_alive and maniac_alive and len(town_alive) <= 1:
            self.winner = Winner.MANIAC
            self.phase = Phase.ENDED
            return
        non_mafia = len(town_alive) + len(maniac_alive)
        if len(mafia_alive) >= non_mafia:
            self.winner = Winner.MAFIA
            self.phase = Phase.ENDED

    def is_over(self) -> bool:
        return self.phase == Phase.ENDED
