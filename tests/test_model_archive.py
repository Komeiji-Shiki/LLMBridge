"""模型归档核心逻辑测试

覆盖 core/model_archive.py 的纯函数：
- is_model_archived：dict / list 两种配置形态
- set_archive_flags：批量归档/恢复、list 全元素处理、幂等
- find_inactive_models：天数阈值、多候选 key 匹配、无记录跳过、已归档跳过
- build_last_used_map：SQLite 优先、内存兜底
"""
import time

import pytest

from core.model_archive import (
    is_model_archived,
    set_archive_flags,
    find_inactive_models,
    build_last_used_map,
)


# ==================== is_model_archived ====================

class TestIsModelArchived:
    def test_dict_archived_true(self):
        assert is_model_archived({"archived": True, "api_type": "direct_api"}) is True

    def test_dict_archived_false(self):
        assert is_model_archived({"archived": False}) is False

    def test_dict_no_archived_key(self):
        assert is_model_archived({"api_type": "direct_api"}) is False

    def test_list_first_element_archived(self):
        assert is_model_archived([{"archived": True}, {"archived": True}]) is True

    def test_list_first_not_archived(self):
        # 语义与 models_api._is_archived 一致：只看第一个元素
        assert is_model_archived([{"archived": False}, {"archived": True}]) is False

    def test_empty_list(self):
        assert is_model_archived([]) is False

    def test_invalid_types(self):
        assert is_model_archived(None) is False
        assert is_model_archived("string") is False
        assert is_model_archived(42) is False


# ==================== set_archive_flags ====================

class TestSetArchiveFlags:
    def test_archive_single_dict(self):
        model_map = {"m1": {"api_type": "direct_api"}}
        new_map, changed = set_archive_flags(model_map, ["m1"], True)
        assert new_map["m1"]["archived"] is True
        assert changed == ["m1"]

    def test_restore_single_dict(self):
        model_map = {"m1": {"api_type": "direct_api", "archived": True}}
        new_map, changed = set_archive_flags(model_map, ["m1"], False)
        assert "archived" not in new_map["m1"]
        assert changed == ["m1"]

    def test_archive_all_list_elements(self):
        model_map = {"m1": [{"mode": "direct_chat"}, {"mode": "battle"}]}
        new_map, changed = set_archive_flags(model_map, ["m1"], True)
        assert new_map["m1"][0]["archived"] is True
        assert new_map["m1"][1]["archived"] is True
        assert changed == ["m1"]

    def test_restore_all_list_elements(self):
        model_map = {"m1": [{"archived": True}, {"archived": True}]}
        new_map, changed = set_archive_flags(model_map, ["m1"], False)
        assert "archived" not in new_map["m1"][0]
        assert "archived" not in new_map["m1"][1]
        assert changed == ["m1"]

    def test_idempotent_no_change(self):
        model_map = {"m1": {"archived": True}}
        new_map, changed = set_archive_flags(model_map, ["m1"], True)
        assert changed == []
        assert new_map == model_map

    def test_missing_model_ignored(self):
        model_map = {"m1": {"api_type": "direct_api"}}
        new_map, changed = set_archive_flags(model_map, ["nope"], True)
        assert changed == []
        assert new_map == model_map

    def test_mixed_batch(self):
        model_map = {
            "active": {"api_type": "direct_api"},
            "archived": {"api_type": "direct_api", "archived": True},
        }
        new_map, changed = set_archive_flags(model_map, ["active", "archived"], True)
        assert new_map["active"]["archived"] is True
        assert new_map["archived"]["archived"] is True
        assert sorted(changed) == ["active"]

    def test_original_map_not_mutated(self):
        model_map = {"m1": {"api_type": "direct_api"}}
        set_archive_flags(model_map, ["m1"], True)
        assert "archived" not in model_map["m1"]


# ==================== find_inactive_models ====================

class TestFindInactiveModels:
    def _map(self):
        return {
            "old-model": {"api_type": "direct_api", "display_name": "old-display"},
            "recent-model": {"api_type": "direct_api"},
            "archived-model": {"api_type": "direct_api", "archived": True},
            "never-called": {"api_type": "direct_api"},
        }

    def test_basic_inactive(self):
        now = time.time()
        last_used = {
            "old-display": now - 40 * 86400,   # 40 天前，通过 display_name 命中
            "recent-model": now - 5 * 86400,   # 5 天前
        }
        inactive = find_inactive_models(self._map(), last_used, 30)
        assert inactive == ["old-model"]

    def test_archived_skipped(self):
        now = time.time()
        last_used = {"archived-model": now - 400 * 86400}
        assert find_inactive_models(self._map(), last_used, 30) == []

    def test_never_called_skipped(self):
        # 无调用记录的模型（可能是新配置）不归档
        last_used = {"old-display": time.time() - 400 * 86400}
        inactive = find_inactive_models(self._map(), last_used, 30)
        assert "never-called" not in inactive

    def test_boundary_exact_cutoff(self):
        # 恰好等于 cutoff 的模型：last_ts < cutoff 才算闲置，等于不算
        model_map = {"m1": {"api_type": "direct_api"}}
        last_used = {"m1": time.time() - 30 * 86400}
        assert find_inactive_models(model_map, last_used, 30) == []

    def test_multiple_candidates_take_max(self):
        # 同一模型在统计里同时有 display_name 和 model_id 记录，取最大时间戳
        now = time.time()
        model_map = {"m1": {"api_type": "direct_api", "display_name": "disp", "model_id": "mid"}}
        last_used = {"disp": now - 60 * 86400, "mid": now - 3 * 86400}
        assert find_inactive_models(model_map, last_used, 30) == []

    def test_zero_days_returns_empty(self):
        assert find_inactive_models(self._map(), {"old-model": 1}, 0) == []

    def test_list_config_candidates(self):
        now = time.time()
        model_map = {"m1": [{"mode": "direct_chat", "display_name": "lst-disp"}, {"mode": "battle"}]}
        last_used = {"lst-disp": now - 45 * 86400}
        assert find_inactive_models(model_map, last_used, 30) == ["m1"]


# ==================== build_last_used_map ====================

class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return self._rows


class _FakeStatsDB:
    """模拟 StatsDB：启用 + 返回固定行"""
    def __init__(self, rows, enabled=True):
        self._rows = rows
        self.enabled = enabled

    def _check_enabled(self):
        return self.enabled

    def _get_connection(self):
        class _Conn:
            def __init__(self, rows):
                self._rows = rows
                self.cursor = lambda: _FakeCursor(self._rows)
        return _Conn(self._rows)


class _FakeMonitoring:
    def __init__(self, recent=None):
        self.recent_requests = recent or []


class TestBuildLastUsedMap:
    def test_sqlite_priority(self):
        stats_db = _FakeStatsDB([("m1", 100.0), ("m2", 200.0)])
        result = build_last_used_map(stats_db, _FakeMonitoring())
        assert result == {"m1": 100.0, "m2": 200.0}

    def test_sqlite_disabled_falls_back_to_memory(self):
        recent = [
            {"model": "m1", "timestamp": 100.0},
            {"model": "m2", "timestamp": 200.0},
            {"model": "m1", "timestamp": 300.0},  # 同模型取最大
        ]
        result = build_last_used_map(_FakeStatsDB([], enabled=False), _FakeMonitoring(recent))
        assert result == {"m1": 300.0, "m2": 200.0}

    def test_both_empty(self):
        assert build_last_used_map(_FakeStatsDB([], enabled=False), _FakeMonitoring([])) == {}

    def test_none_dependencies(self):
        assert build_last_used_map(None, None) == {}
