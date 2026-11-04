from core.model_orchestrator import DYNAMIC_CPUSETS, MAX_CONCURRENT_INSTANCES, choose_cpuset


def test_each_model_gets_its_own_cpus():
    assert len(DYNAMIC_CPUSETS) >= MAX_CONCURRENT_INSTANCES
    assert choose_cpuset(set(), 32) == "8-11"
    assert choose_cpuset({"8-11"}, 32) == "12-15"
    assert choose_cpuset({"8-11", "12-15"}, 32) == "16-19"
    assert choose_cpuset({"12-15", None}, 32) == "8-11"


def test_a_range_comes_free_again():
    assert choose_cpuset({"12-15"}, 32) == "8-11"


def test_a_smaller_server_shares_what_it_has():
    assert choose_cpuset({"8-11"}, 16) == "12-15"
    assert choose_cpuset({"8-11", "12-15"}, 16) == "8-11"
    assert choose_cpuset(set(), 8) == "0-7"
