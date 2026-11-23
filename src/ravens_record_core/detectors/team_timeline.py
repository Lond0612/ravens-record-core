"""
チーム戦の試合の流れ（タイムライン）をまとめる（v1.8〜。読み取りは team_hud.py）

試合中、約 1 秒ごとに画面を渡す（observe）。試合の終わりに、リザルト画面の読み取りと合わせて、記録にする（finish）。

時刻は「試合の何秒目か」（残り時間 3:00 からの経過。残り時間が読めない画面は、直前に読めたところからの経過で補う）。

出来事（timeline。時刻順。短い配列で持つ）:
  ["p", t, 自チームの合計, 相手の合計, 自チームの増え, 自分の SCORE の増え, 相手の増え]
      ポイントの変化。自チームの増え − 自分の増え = 味方（自分以外）の得たポイント。読めなかったところは null
  ["d", t, "a" | "b", 番号]   撃破（撃破された機体。a = ALPHA・b = BETA）
  ["q", t, "a" | "b", 番号]   プライオリティ・ターゲットが、その機体に移った
  ["r", t]                    自分が撃破された（MAIN SYSTEM REBOOTING が出た）
  ["u", t]                    自分が再出撃した（REBOOTING が消えた）
ポイントは、2 回続けて同じ値が読めたら採用する（減らない・1 回で 400 を超えて増えない）。最後の合計は、リザルト画面の値で補う。
流れる表示の行は数秒出ている。直近 FEED_MEMORY_SEC 秒に見た同じ行（陣営・番号・種類）の数を超えた分だけを、新しい出来事にする
（行の動き出しなどで、ある回だけ読めなかった行を、次の回に二重に数えないため。同じ機体が数秒のうちに 2 回撃破されることは無い）。

集計（summary）:
  team_kills / team_deaths: 相手・自チームの撃破された数（流れる表示の色）
  own_deaths: 自分の撃破された数（自分の番号の「撃破」と、それに当たらない REBOOTING。試合の終わりぎわの撃破は REBOOTING が出ない）
  own_slot / own_slot_basis: 自分の番号と、その決め方（emblem: エンブレム / death: 自分が撃破された時刻の「撃破」の番号）
  priority_sec: {"a": {"1": 秒, ...}, "b": {...}} 陣営・番号ごとに、プライオリティだった秒数
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Optional

import numpy as np

from ravens_record_core.detectors import match_end, team_hud

MATCH_SEC = 180
MAX_STEP = 400          # 1 回の読み取りで増えうるポイントの上限（それより大きい増えは読み誤り）
MERGE_SEC = 2.5         # この秒数以内のポイントの変化は、1 つの出来事にまとめる
DEATH_MATCH_SEC = 6.0   # 自分の REBOOTING と、自チームの「撃破」を同じ出来事とみなす時刻の差（REBOOTING は撃破の 4 秒ほどあとに出る）
FEED_MEMORY_SEC = 5.0   # 流れる表示の行を、同じ行とみなす時間
SIDE_CODE = {"ALPHA": "a", "BETA": "b"}


class _Confirmed:
    """2 回続けて同じ値が読めたら採用する数字（減らない・急に増えすぎない）"""

    def __init__(self) -> None:
        self.value = 0
        self.pending: Optional[int] = None
        self.pending_t: Optional[float] = None

    def feed(self, v: Optional[int], t: float) -> Optional[tuple[float, int, int]]:
        """新しく採用したら (最初に読めた時刻, 前の値, 新しい値)"""
        if v is None or v == self.value or v < self.value or v - self.value > MAX_STEP:
            self.pending = None
            return None
        if self.pending == v:
            old, self.value, self.pending = self.value, v, None
            return self.pending_t, old, v
        self.pending, self.pending_t = v, t
        return None


class TeamTimeline:
    def __init__(self, emblem: Optional[np.ndarray] = None, debug: Optional[list] = None) -> None:
        self.emblem = emblem
        self.debug = debug   # 渡すと、1 回ごとの読み取りを足す（調査用。monitor/team.py の TeamDebug）
        self.started = False
        self.offset: Optional[float] = None      # 実際の時刻 − 試合の秒（残り時間から）
        self.last_t = 0.0
        self.fields = {k: _Confirmed() for k in ("own", "opp", "score")}
        self.changes: list[tuple[float, str, int, int]] = []
        self.events: list[list] = []
        self.feed_seen: deque = deque()   # (時刻, その回に見えた行の数え)
        self.rebooting = False
        self.hud_side: Optional[str] = None
        self.emblem_slot: Optional[int] = None
        self.emblem_candidates: list[int] = []
        self.emblem_tries = 0

    # ---- 試合中 ----------------------------------------------------------

    def observe(self, wall: float, frame: np.ndarray) -> None:
        if not team_hud.available() or frame is None:
            return
        f = team_hud.to_1080(frame)
        remaining = team_hud.read_timer(f)
        if remaining is not None:
            t = float(MATCH_SEC - remaining)
            est = wall - t
            # 残り時間は 1 秒刻みなので、実際の時刻との差は、いちばん大きいもの（秒の変わり目に近いもの）を使う
            self.offset = est if self.offset is None else max(self.offset, est) if abs(est - self.offset) < 3 else est
            self.started = True
        elif not self.started or self.offset is None:
            return
        t = round(min(max(wall - self.offset, 0.0), MATCH_SEC + 5), 1)
        self.last_t = t

        if self.hud_side is None:
            self.hud_side = match_end.read_own_side(frame)
        emblem_scores = None
        if self.emblem is not None and self.emblem_slot is None and self.emblem_tries < 20:
            self.emblem_tries += 1
            emblem_scores = team_hud.emblem_scores(f, self.emblem)
            slot, candidates = team_hud.own_slot_from_emblem(emblem_scores)
            if slot is not None:
                self.emblem_slot = slot
            elif candidates:
                self.emblem_candidates = sorted(set(self.emblem_candidates) | set(candidates))

        reads = {}
        for field, conf in self.fields.items():
            reads[field] = team_hud.read_field(f, field)
            got = conf.feed(reads[field], t)
            if got is not None:
                self.changes.append((got[0], field, got[1], got[2]))

        feed = team_hud.read_feed(f)
        for kind, side, slot in self._new_rows(t, feed):
            self.events.append(["q" if kind == "priority" else "d", t, SIDE_CODE[side], slot])

        rebooting = team_hud.is_rebooting(f)
        if self.debug is not None:
            self.debug.append({"wall": round(wall, 2), "t": t, "timer": remaining, **reads, "feed": feed, "reboot": rebooting,
                               "emblem": [round(s, 3) for s in emblem_scores] if emblem_scores else None, "side": self.hud_side})
        if rebooting != self.rebooting:
            self.events.append(["r" if rebooting else "u", t])
            self.rebooting = rebooting

    def _new_rows(self, t: float, rows: list) -> list:
        """直近 FEED_MEMORY_SEC 秒に見た数を超えた分の行"""
        while self.feed_seen and t - self.feed_seen[0][0] > FEED_MEMORY_SEC:
            self.feed_seen.popleft()
        now = Counter(rows)
        new = []
        for row, n in now.items():
            seen = max((c[row] for _, c in self.feed_seen), default=0)
            new += [row] * max(0, n - seen)
        self.feed_seen.append((t, now))
        return new

    # ---- 試合の終わり -------------------------------------------------------

    def finish(self, own_side: Optional[str], final_own: Optional[int], final_opp: Optional[int],
               final_score: Optional[int]) -> Optional[dict]:
        """記録にする {"timeline": [...], "summary": {...}}。試合中の表示を一度も読めなければ None"""
        if not self.started:
            return None
        own_side = own_side or self.hud_side
        # 最後の合計をリザルト画面の値で補う（最後の撃破が、読む前に試合が終わったときなど）
        end_t = float(MATCH_SEC)
        for field, final in (("own", final_own), ("opp", final_opp), ("score", final_score)):
            conf = self.fields[field]
            if final is not None and final > conf.value:
                self.changes.append((end_t, field, conf.value, final))
                conf.value = final
        timeline = self._point_events() + self.events
        timeline.sort(key=lambda e: (e[1], "pdqru".index(e[0])))
        return {"timeline": timeline, "summary": self._summary(timeline, own_side)}

    def _point_events(self) -> list[list]:
        """ポイントの変化を、近い時刻どうしまとめて ["p", t, 自チーム, 相手, 自チームの増え, 自分の増え, 相手の増え]"""
        out: list[list] = []
        totals = {"own": 0, "opp": 0}
        group: list[tuple[float, str, int, int]] = []

        def flush() -> None:
            if not group:
                return
            d = {"own": 0, "opp": 0, "score": 0}
            for _, field, old, new in group:
                d[field] += new - old
                if field in totals:
                    totals[field] = new
            out.append(["p", round(group[0][0], 1), totals["own"], totals["opp"], d["own"], d["score"], d["opp"]])
            group.clear()

        for change in sorted(self.changes):
            if group and change[0] - group[0][0] > MERGE_SEC:
                flush()
            group.append(change)
        flush()
        return out

    def _summary(self, timeline: list[list], own_side: Optional[str]) -> dict:
        own_code = SIDE_CODE.get(own_side) if own_side else None
        downs = [e for e in timeline if e[0] == "d"]
        reboots = [e[1] for e in timeline if e[0] == "r"]
        slot, basis = self.emblem_slot, "emblem" if self.emblem_slot else None
        if slot is None and own_code:
            # 自分が撃破された時刻に出た、自チームの「撃破」の番号（同じエンブレムの味方がいるときは、その候補の中から）
            hits = Counter(e[3] for e in downs for r in reboots
                           if e[2] == own_code and abs(e[1] - r) <= DEATH_MATCH_SEC
                           and (not self.emblem_candidates or e[3] in self.emblem_candidates))
            if hits:
                best, n = hits.most_common(1)[0]
                if n * 2 > sum(hits.values()):
                    slot, basis = best, "death"
        own_downs = [e[1] for e in downs if own_code and slot is not None and e[2] == own_code and e[3] == slot]
        own_deaths = len(own_downs) + sum(1 for r in reboots if not any(abs(r - d) <= DEATH_MATCH_SEC for d in own_downs))
        priority: dict[str, dict[str, float]] = {}
        holder: dict[str, tuple[int, float]] = {}
        for e in timeline:
            if e[0] != "q":
                continue
            side, s, t = e[2], e[3], e[1]
            if side in holder:
                prev, since = holder[side]
                priority.setdefault(side, {}).setdefault(str(prev), 0.0)
                priority[side][str(prev)] += t - since
            holder[side] = (s, t)
        for side, (s, since) in holder.items():
            priority.setdefault(side, {}).setdefault(str(s), 0.0)
            priority[side][str(s)] += max(0.0, MATCH_SEC - since)
        priority = {side: {k: round(v) for k, v in m.items()} for side, m in priority.items()}
        return {
            "team_kills": sum(1 for e in downs if own_code and e[2] != own_code) if own_code else None,
            "team_deaths": sum(1 for e in downs if own_code and e[2] == own_code) if own_code else None,
            "own_deaths": own_deaths,
            "own_slot": slot,
            "own_slot_basis": basis,
            "priority_sec": priority,
        }

