"""航段分时航时管制测试。

规则：
* 调度员以航段编号提交一组半开分钟区间 [起, 止) 及正整数通行分钟，整组
  替换该航段旧管制并返回生效清单；空组恢复基础航时；
* 未知航段、非法区间（止<=起、负分钟、非正通行分钟）及组内重叠一律拒绝，
  失败不改旧管制；
* 船在进入航段的分钟选定航时，途中跨越管制边界也不变；未命中区间沿用
  航段基础航时；
* 单船与批次预排/确认按实际航时计算抵达、潮位/桥高与航段独占；确认在
  原事务中用当时管制重算，变化导致无解或不一致按 PLAN_MISMATCH 拒绝；
* 已确认快照的既有占用时长不随管制变化；潮位复核仍按原快照占用，改期
  用当前管制求解。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _put_controls(client, seg_id, controls):
    return client.put(f"/api/segments/{seg_id}/controls",
                      json={"controls": controls})


def _get_controls(client, seg_id):
    r = client.get(f"/api/segments/{seg_id}/controls")
    assert r.status_code == 200, r.text
    return r.json()


def _plan(client, **over):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
    }
    body.update(over)
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, plan):
    return client.post("/api/trips/confirm", json=plan)


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20, draft=1.0, height=0.0, margin=0.0):
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, draft, height, margin,
         arrival, json.dumps(itinerary)),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


def _ab_segment(client, travel_time=2, depth=99.0):
    add_node(client, "A")
    add_node(client, "B")
    return add_segment(client, "A", "B", travel_time=travel_time, depth=depth)


# ---------------------------------------------------------------------------
# 1. 提交管制：整组替换并返回生效清单；GET 可查
# ---------------------------------------------------------------------------

def test_put_controls_replaces_group_and_returns_effective_list(client):
    seg = _ab_segment(client, travel_time=2)

    r = _put_controls(client, seg, [
        {"start_minute": 60, "end_minute": 120, "travel_time": 5},
        {"start_minute": 0, "end_minute": 30, "travel_time": 7},
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    # 生效清单按区间起点升序返回，并带航段基础航时
    assert data["seg_id"] == seg
    assert data["base_travel_time"] == 2
    assert data["controls"] == [
        {"start_minute": 0, "end_minute": 30, "travel_time": 7},
        {"start_minute": 60, "end_minute": 120, "travel_time": 5},
    ]
    assert _get_controls(client, seg)["controls"] == data["controls"]

    # 再次提交：整组替换，旧区间不再生效
    r = _put_controls(client, seg, [
        {"start_minute": 10, "end_minute": 20, "travel_time": 9},
    ])
    assert r.status_code == 200
    assert r.json()["controls"] == [
        {"start_minute": 10, "end_minute": 20, "travel_time": 9},
    ]
    assert _get_controls(client, seg)["controls"] == [
        {"start_minute": 10, "end_minute": 20, "travel_time": 9},
    ]


def test_empty_group_restores_base_travel_time(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 100, "travel_time": 9},
    ])
    r = _put_controls(client, seg, [])
    assert r.status_code == 200
    assert r.json()["controls"] == []
    assert _get_controls(client, seg)["controls"] == []

    # 恢复基础航时：0 进入、2 抵达
    plan = _plan(client, revision=rev)
    assert [l["travel_time"] for l in plan["itinerary"]] == [2]
    assert plan["arrival_minute"] == 2


# ---------------------------------------------------------------------------
# 2. 拒绝：未知航段 / 非法区间 / 组内重叠；失败不改旧管制
# ---------------------------------------------------------------------------

def test_unknown_segment_rejected(client):
    r = _put_controls(client, 999, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
    ])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"
    assert r.json()["error"]["details"]["seg_id"] == 999

    r = client.get("/api/segments/999/controls")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"


def test_invalid_intervals_rejected_and_old_controls_kept(client):
    seg = _ab_segment(client, travel_time=2)
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
    ])

    bad_bodies = [
        # 止 <= 起
        {"controls": [{"start_minute": 5, "end_minute": 5, "travel_time": 3}]},
        {"controls": [{"start_minute": 6, "end_minute": 5, "travel_time": 3}]},
        # 负分钟
        {"controls": [{"start_minute": -1, "end_minute": 5, "travel_time": 3}]},
        # 通行分钟非正整数
        {"controls": [{"start_minute": 0, "end_minute": 5, "travel_time": 0}]},
        {"controls": [{"start_minute": 0, "end_minute": 5, "travel_time": -2}]},
        {"controls": [{"start_minute": 0, "end_minute": 5, "travel_time": 1.5}]},
    ]
    for body in bad_bodies:
        r = client.put(f"/api/segments/{seg}/controls", json=body)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 旧管制原样保留
    assert _get_controls(client, seg)["controls"] == [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
    ]


def test_overlapping_group_rejected_and_old_controls_kept(client):
    seg = _ab_segment(client, travel_time=2)
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
    ])

    # 组内重叠（含相同起点的重复区间）
    for controls in [
        [{"start_minute": 0, "end_minute": 10, "travel_time": 3},
         {"start_minute": 5, "end_minute": 15, "travel_time": 4}],
        [{"start_minute": 0, "end_minute": 10, "travel_time": 3},
         {"start_minute": 0, "end_minute": 5, "travel_time": 4}],
        [{"start_minute": 0, "end_minute": 10, "travel_time": 3},
         {"start_minute": 2, "end_minute": 8, "travel_time": 4}],
    ]:
        r = _put_controls(client, seg, controls)
        assert r.status_code == 422, (controls, r.text)
        assert r.json()["error"]["code"] == "CONTROL_WINDOW_OVERLAP"

    # 相邻（半开区间端点相接）不算重叠
    r = _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
        {"start_minute": 10, "end_minute": 20, "travel_time": 4},
    ])
    assert r.status_code == 200, r.text
    assert _get_controls(client, seg)["controls"] == [
        {"start_minute": 0, "end_minute": 10, "travel_time": 3},
        {"start_minute": 10, "end_minute": 20, "travel_time": 4},
    ]


# ---------------------------------------------------------------------------
# 3. 规划：进入分钟选定航时；未命中区间用基础航时
# ---------------------------------------------------------------------------

def test_plan_uses_controlled_travel_time_when_entry_hits_interval(client):
    seg = _ab_segment(client, travel_time=2, depth=4.0)
    # 0..4 低潮（吃水不够），5..19 够
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    _put_controls(client, seg, [
        {"start_minute": 5, "end_minute": 10, "travel_time": 6},
    ])

    plan = _plan(client, draft=6.0, margin=0.5, revision=rev)
    # 最早 5 进入，命中 [5,10) -> 航时 6，占 5..10，11 抵达
    assert [l["start_minute"] for l in plan["itinerary"]] == [5]
    assert [l["travel_time"] for l in plan["itinerary"]] == [6]
    assert plan["arrival_minute"] == 11


def test_entry_minute_selects_travel_time_even_when_crossing_boundary(client):
    seg = _ab_segment(client, travel_time=8)
    rev = seed_initial_revision(_level(0, 29, 3.0))
    # 管制 [0,10) 航时 2：9 进入仍按 2 分钟（占用 9,10，途中跨越边界 10 不变）
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 2},
    ])

    plan = _plan(client, start_minute=9, window_minutes=21, revision=rev)
    assert [l["start_minute"] for l in plan["itinerary"]] == [9]
    assert [l["travel_time"] for l in plan["itinerary"]] == [2]
    assert plan["arrival_minute"] == 11

    # 10 进入未命中区间 -> 基础航时 8
    plan = _plan(client, start_minute=10, window_minutes=20, revision=rev)
    assert [l["travel_time"] for l in plan["itinerary"]] == [8]
    assert plan["arrival_minute"] == 18

    # 确认 9 进入的船：占用 9,10 共 2 分钟（按进入分钟选定的航时）
    trip = _confirm(client, _plan(client, start_minute=9, window_minutes=21,
                                  revision=rev))
    assert trip.status_code == 201, trip.text
    other = _plan(client, start_minute=9, window_minutes=21, revision=rev)
    # 9/10 进入均与占用冲突；11 进入未命中管制（基础航时 8），占 11..18
    assert [l["start_minute"] for l in other["itinerary"]] == [11]
    assert [l["travel_time"] for l in other["itinerary"]] == [8]
    assert other["arrival_minute"] == 19


def test_controlled_occupancy_window_checked_against_tide(client):
    seg = _ab_segment(client, travel_time=2, depth=4.0)
    # 5..9 潮位够、10..12 低潮、13..19 够；管制 [5,8) 航时 8
    rev = seed_initial_revision({
        **_level(0, 4, 0.0), **_level(5, 9, 3.0),
        **_level(10, 12, 0.0), **_level(13, 19, 3.0),
    })
    _put_controls(client, seg, [
        {"start_minute": 5, "end_minute": 8, "travel_time": 8},
    ])

    plan = _plan(client, draft=6.0, margin=0.5, revision=rev)
    # 5 进入航时 8 -> 占 5..12，其中 10..12 低潮不可行；
    # 8 进入未命中区间（[5,8) 不含 8）-> 基础航时 2，占 8,9 可行，10 抵达
    assert [l["start_minute"] for l in plan["itinerary"]] == [8]
    assert [l["travel_time"] for l in plan["itinerary"]] == [2]
    assert plan["arrival_minute"] == 10


# ---------------------------------------------------------------------------
# 4. 确认：原事务中用当时管制重算；变化导致不一致/无解 -> PLAN_MISMATCH
# ---------------------------------------------------------------------------

def test_confirm_recalculates_with_current_controls_mismatch(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _plan(client, revision=rev)
    assert [l["travel_time"] for l in plan["itinerary"]] == [2]

    # 规划后新增管制：0..5 进入航时 6
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 5, "travel_time": 6},
    ])
    r = _confirm(client, plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []  # 不保存

    # 按当时管制重新规划后可确认
    replan = _plan(client, revision=rev)
    assert [l["travel_time"] for l in replan["itinerary"]] == [6]
    assert replan["arrival_minute"] == 6
    ok = _confirm(client, replan)
    assert ok.status_code == 201, ok.text
    assert [l["travel_time"] for l in ok.json()["itinerary"]] == [6]


def test_confirm_no_solution_after_control_change(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _plan(client, start_minute=0, window_minutes=4, revision=rev)
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]

    # 管制把 0 进入的航时拉成 10：窗口 4 内任何进入分钟都无法抵达
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 4, "travel_time": 10},
    ])
    r = _confirm(client, plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert r.json()["error"]["details"]["reason"] == "no_feasible_itinerary"
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 5. 批次：预排与确认同样按实际航时；全成全败与同键重放保持
# ---------------------------------------------------------------------------

def test_batch_plan_and_confirm_with_controls(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 4},
    ])

    ship = {"origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0}
    r = client.post("/api/trips/batch/plan",
                    json={"revision": rev, "ships": [ship, ship]})
    assert r.status_code == 200, r.text
    data = r.json()
    # 船 0：0 进入航时 4，占 0..3；船 1 避开拟占用，4 进入（仍命中管制）
    assert [l["start_minute"] for l in data["ships"][0]["itinerary"]] == [0]
    assert [l["travel_time"] for l in data["ships"][0]["itinerary"]] == [4]
    assert [l["start_minute"] for l in data["ships"][1]["itinerary"]] == [4]
    assert data["ships"][1]["arrival_minute"] == 8

    body = {
        "revision": rev,
        "idempotency_key": "ctl-batch-1",
        "ships": [
            {**ship, "itinerary": s["itinerary"]} for s in data["ships"]
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["replayed"] is False
    assert len(r.json()["trips"]) == 2

    # 管制变化不影响同键重放
    _put_controls(client, seg, [])
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True


def test_batch_confirm_mismatch_after_control_change_rolls_back(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    ship = {"origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0}
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": [ship, ship]}).json()

    # 预排后改管制：复算结果必然不一致 -> PLAN_MISMATCH，全批不保存
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 10, "travel_time": 5},
    ])
    body = {
        "revision": rev,
        "idempotency_key": "ctl-batch-2",
        "ships": [
            {**ship, "itinerary": s["itinerary"]} for s in plan["ships"]
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 6. 已确认快照：既有占用时长不随管制变化
# ---------------------------------------------------------------------------

def test_confirmed_snapshot_occupancy_unaffected_by_control_change(client):
    seg = _ab_segment(client, travel_time=2)
    rev = seed_initial_revision(_level(0, 29, 3.0))

    # 快照：0 进入航时 2，占 0,1
    trip = _confirm(client, _plan(client, revision=rev, window_minutes=30))
    assert trip.status_code == 201, trip.text
    assert [l["travel_time"] for l in trip.json()["itinerary"]] == [2]

    # 管制改为 0..100 进入航时 10：快照占用仍是 0,1（不随管制变化）
    _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 100, "travel_time": 10},
    ])
    plan = _plan(client, revision=rev, window_minutes=30)
    # 2 进入（命中管制航时 10）占 2..11，不与快照的 0,1 重叠 -> 可行
    assert [l["start_minute"] for l in plan["itinerary"]] == [2]
    assert [l["travel_time"] for l in plan["itinerary"]] == [10]
    assert plan["arrival_minute"] == 12


# ---------------------------------------------------------------------------
# 7. 复核按原快照占用；改期用当前管制求解
# ---------------------------------------------------------------------------

def test_review_keeps_snapshot_occupancy_and_reschedule_uses_current_controls(client):
    seg = _ab_segment(client, travel_time=3, depth=4.0)
    # 潮位：0..4 低潮，5..19 够；快照 5 进入航时 3，占 5,6,7
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    mine = _raw_insert_trip(rev, [
        {"seg_id": seg, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 更正：5..7 低潮 -> 原行程在分钟 5 违规；8 起潮位恢复
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 8)}})
    # 当前管制：8..20 进入航时 5
    _put_controls(client, seg, [
        {"start_minute": 8, "end_minute": 20, "travel_time": 5},
    ])

    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    # 复核仍按快照原占用分钟（5,6,7）：首次违规为分钟 5
    assert data["safe"] is False
    assert data["first_violation"]["minute"] == 5
    assert data["first_violation"]["reasons"] == ["depth"]
    # 改期用当前管制：8 进入航时 5，13 抵达
    rs = data["reschedule"]
    assert rs is not None
    assert rs["itinerary"] == [
        {"seg_id": seg, "origin": "A", "destination": "B",
         "start_minute": 8, "end_minute": 13, "travel_time": 5},
    ]
    assert rs["arrival_minute"] == 13
    assert rs["changed_leg_count"] == 1

    # 快照不改写：仍是 5 进入、航时 3
    g = client.get(f"/api/trips/{mine}").json()
    assert [l["start_minute"] for l in g["itinerary"]] == [5]
    assert [l["travel_time"] for l in g["itinerary"]] == [3]


def test_reschedule_without_controls_uses_base_travel_time(client):
    seg = _ab_segment(client, travel_time=3, depth=4.0)
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    mine = _raw_insert_trip(rev, [
        {"seg_id": seg, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 8)}})
    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    rs = data["reschedule"]
    # 无管制：基础航时 3，8 进入 11 抵达
    assert [l["travel_time"] for l in rs["itinerary"]] == [3]
    assert rs["arrival_minute"] == 11
