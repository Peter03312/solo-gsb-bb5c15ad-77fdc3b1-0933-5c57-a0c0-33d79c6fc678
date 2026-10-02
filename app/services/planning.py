"""潮汐通航行程规划核心算法。

时间轴为绝对"分钟"，与潮位表 minute 列一致。船舶可在节点的任意整分钟
等待；航段一旦进入即连续占用 travel_time 个分钟（进入分钟 d 占用
d, d+1, ..., d+t-1，于 d+t 抵达下一节点）。

约束（占用的每一分钟均须满足，按修订的预测误差带取保守极值）：
  * 深度：静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量（真值可能偏低）
  * 桥梁：桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量（真值可能偏高，有桥时）
  * 独占：同一航段同一分钟已被其他**已确认**行程占用时不可再进入（进入
    分钟 d 至抵达前一分钟 d+t-1 独占该航段；节点等待不占用航段）
等待不占用航段，不参与校验。

择优：最早抵达；同一抵达时刻按航段编号序列字典序（小者优先）。
算法：窗口 <= 1440 分钟，按时间展开 DP，每个 (节点, 分钟) 只保留字典序
最小的航段编号序列。该性质可安全剪枝——任何延续在相同状态上叠加后，
前缀字典序大小关系不变。
"""
import json
from dataclasses import dataclass
from typing import Optional

from .. import config
from ..errors import ApiError, no_itinerary, tide_missing, unknown_node


@dataclass(frozen=True)
class Segment:
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float]


def _load_segments(conn) -> dict[int, Segment]:
    rows = conn.execute(
        "SELECT seg_id, origin, destination, travel_time, depth, clearance"
        " FROM segments ORDER BY seg_id"
    ).fetchall()
    return {
        r["seg_id"]: Segment(
            r["seg_id"], r["origin"], r["destination"],
            r["travel_time"], r["depth"], r["clearance"],
        )
        for r in rows
    }


def _load_tide(conn, revision: int, lo: int, hi: int) -> dict[int, tuple[float, float]]:
    """返回修订 revision 在 [lo, hi] 闭区间内的 {minute: (level, error)}。"""
    rows = conn.execute(
        "SELECT minute, level, error FROM tide_values"
        " WHERE revision = ? AND minute BETWEEN ? AND ?",
        (revision, lo, hi),
    ).fetchall()
    return {r["minute"]: (r["level"], r["error"]) for r in rows}


def _minute_ok(seg: Segment, tide: float, error: float,
               draft: float, height: float, margin: float) -> bool:
    """航段在某一分钟是否满足深度/净空约束（按误差带取保守极值）。"""
    eps = config.EPS
    if seg.depth + tide - error + eps < draft + margin:
        return False
    if seg.clearance is not None:
        if seg.clearance - tide - error + eps < height + margin:
            return False
    return True


def load_occupancy(conn, lo: int, hi: int, exclude_trip_id: Optional[int] = None
                   ) -> dict[int, set[int]]:
    """读取 [lo, hi] 闭区间内全部已确认行程的航段占用。

    返回 {seg_id: {占用分钟, ...}}。每段从进入分钟 d 起占用
    d, d+1, …, d+travel_time-1（抵达分钟不占用）。既有快照即使彼此重叠也
    原样保留——它们就是后续规划必须避开的占用。
    exclude_trip_id 用于改期复核：建议须避开其他已确认快照，但排除自身。
    """
    sql = "SELECT itinerary FROM confirmed_trips"
    params: list = []
    if exclude_trip_id is not None:
        sql += " WHERE trip_id <> ?"
        params.append(exclude_trip_id)
    occupied: dict[int, set[int]] = {}
    for row in conn.execute(sql, params).fetchall():
        for leg in json.loads(row["itinerary"]):
            d, t = leg["start_minute"], leg["travel_time"]
            end = d + t - 1
            if end < lo or d > hi:
                continue
            occupied.setdefault(leg["seg_id"], set()).update(
                range(max(lo, d), min(hi, end) + 1)
            )
    return occupied


def _build_departure_feasible(
    segs: list[Segment],
    tide: dict[int, float],
    start: int,
    window: int,
    draft: float,
    height: float,
    margin: float,
    blocked: Optional[dict[int, set[int]]] = None,
) -> dict[int, list[bool]]:
    """每条航段每个可能进入分钟（窗口内局部下标）是否可行。

    不可行原因有二：潮位误差带不满足水深/净空，或该占用区间与其他已确认
    行程的占用（blocked：{seg_id: {绝对分钟}}）重叠。用"不可行分钟"前缀
    和，令一次区间查询为 O(1)。
    """
    blocked = blocked or {}
    feasible: dict[int, list[bool]] = {}
    horizon = start + window

    for seg in segs:
        seg_blocked = blocked.get(seg.seg_id, ())
        # bad[i] 对应绝对分钟 start+i
        bad = [0] * window
        for i in range(window):
            m = start + i
            val = tide.get(m)
            if (val is None
                    or not _minute_ok(seg, val[0], val[1], draft, height, margin)
                    or m in seg_blocked):
                bad[i] = 1
        pref = [0] * (window + 1)
        for i in range(window):
            pref[i + 1] = pref[i] + bad[i]

        ok = [False] * (window + 1)  # 局部下标 d-start，0..window
        t = seg.travel_time
        for i in range(0, window - t + 1):
            dep = start + i
            if dep + t > horizon:
                continue
            if pref[i + t] - pref[i] == 0:
                ok[i] = True
        feasible[seg.seg_id] = ok
    return feasible


def find_itinerary(
    conn,
    origin: str,
    destination: str,
    start_minute: int,
    window_minutes: int,
    draft: float,
    height: float,
    margin: float,
    revision: Optional[int] = None,
    blocked: Optional[dict[int, set[int]]] = None,
) -> dict:
    """计算最早抵达行程。

    blocked 给出须避开的已确认占用 {seg_id: {绝对分钟}}；缺省为不避占
    （仅供不需要占用语义的内部调用）。
    未知节点 -> NODE_NOT_FOUND (404)
    窗口内缺潮位 -> MISSING_TIDE_DATA (422)
    无可行行程 -> NO_FEASIBLE_ITINERARY (404)
    """
    # 1) 节点校验
    node_rows = conn.execute(
        "SELECT node_id FROM nodes WHERE node_id IN (?, ?)", (origin, destination)
    ).fetchall()
    known = {r["node_id"] for r in node_rows}
    if origin not in known:
        raise unknown_node(origin)
    if destination not in known:
        raise unknown_node(destination)

    # 2) 修订号（显式指定或取最新）
    if revision is None:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND",
                "系统中不存在任何潮位修订，无法规划",
            )
        revision = row["revision"]
    else:
        row = conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND", f"潮位修订不存在: {revision}",
                {"revision": revision},
            )

    horizon = start_minute + window_minutes

    # 3) 潮位覆盖校验：占用分钟最晚到 horizon-1，窗口逐分钟均须有值
    tide = _load_tide(conn, revision, start_minute, horizon - 1)
    missing = [m for m in range(start_minute, horizon) if m not in tide]
    if missing:
        raise tide_missing(missing)

    # 4) 起终点相同：零航段，立即抵达
    if origin == destination:
        return _result(
            revision, origin, destination, start_minute, window_minutes,
            draft, height, margin, arrival=start_minute, legs=[],
        )

    segs = _load_segments(conn)
    seg_list = list(segs.values())
    out_segs: dict[str, list[Segment]] = {}
    for s in seg_list:
        out_segs.setdefault(s.origin, []).append(s)

    feasible = _build_departure_feasible(
        seg_list, tide, start_minute, window_minutes, draft, height, margin,
        blocked=blocked,
    )

    # 5) 按分钟展开的 DP
    # states[t-local] : 节点 -> 到达该节点该分钟的最优（字典序最小）航段编号序列
    # parents[...]     : 节点 -> (前一局部分钟, 进入所用航段 seg_id 或 None=等待)
    n = window_minutes
    states: list[dict[str, tuple]] = [dict() for _ in range(n + 1)]
    parents: list[dict[str, tuple]] = [dict() for _ in range(n + 1)]
    states[0][origin] = ()

    arrival_local = None
    for i in range(n + 1):
        # 5.1 等待：从 i-1 原样延续到 i（同序列，不加航段编号）
        if i > 0:
            for v, seq in states[i - 1].items():
                if v not in states[i] or seq < states[i][v]:
                    states[i][v] = seq
                    parents[i][v] = (i - 1, None)

        # 5.2 终点首次出现即为最早到达；此刻其序列必为该到达时刻字典序最优
        if destination in states[i]:
            arrival_local = i
            break

        # 5.3 从本分钟出发的航段（船恰好在节点上即可出发，等待后亦可）
        for v, seq in states[i].items():
            for s in out_segs.get(v, []):
                t = s.travel_time
                j = i + t
                if j > n:
                    continue
                if not feasible[s.seg_id][i]:
                    continue
                cand = seq + (s.seg_id,)
                old = states[j].get(s.destination)
                if old is None or cand < old:
                    states[j][s.destination] = cand
                    parents[j][s.destination] = (i, s.seg_id)

    if arrival_local is None:
        raise no_itinerary(origin, destination)

    # 6) 回溯每段起止分钟
    legs: list[tuple[int, int, int]] = []  # (seg_id, 进入局部分钟, 到达局部分钟)
    v, i = destination, arrival_local
    while not (v == origin and i == 0):
        prev_i, seg_id = parents[i][v]
        if seg_id is not None:
            legs.append((seg_id, prev_i, i))
            v = segs[seg_id].origin
        i = prev_i
    legs.reverse()

    leg_dicts = [
        {
            "seg_id": sid,
            "origin": segs[sid].origin,
            "destination": segs[sid].destination,
            "start_minute": start_minute + pi,
            "end_minute": start_minute + ai,
            "travel_time": segs[sid].travel_time,
        }
        for sid, pi, ai in legs
    ]
    return _result(
        revision, origin, destination, start_minute, window_minutes,
        draft, height, margin, arrival=start_minute + arrival_local, legs=leg_dicts,
    )


def _result(revision, origin, destination, start_minute, window_minutes,
            draft, height, margin, arrival, legs) -> dict:
    sailing = sum(l["travel_time"] for l in legs) if legs else 0
    return {
        "revision": revision,
        "origin": origin,
        "destination": destination,
        "start_minute": start_minute,
        "window_minutes": window_minutes,
        "draft": draft,
        "height": height,
        "margin": margin,
        "arrival_minute": arrival,
        "total_wait_minutes": arrival - start_minute - sailing,
        "itinerary": legs,
    }
