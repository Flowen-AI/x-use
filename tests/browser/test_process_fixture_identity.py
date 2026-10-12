"""Deterministic safety checks for native fixture attribution, without processes."""
from test_session_process_cleanup import verified_descendant_pids


def row(parent, created, *, live=True, owned=True):
    return {"parent_pid": parent, "created": created, "live": live, "in_owned_job": owned}


def test_reused_parent_pid_never_attributes_older_foreign_service_tree():
    rows = {10: row(1, 100), 11: row(10, 101), 12: row(11, 102),
            90: row(12, 20, owned=False), 91: row(90, 21, owned=False)}
    assert verified_descendant_pids(rows, 10) == {10, 11, 12}


def test_membership_alone_does_not_replace_parent_creation_ordering():
    rows = {10: row(1, 100), 11: row(10, 101), 90: row(11, 50), 91: row(90, 105)}
    assert verified_descendant_pids(rows, 10) == {10, 11}


def test_creation_ordering_alone_does_not_replace_owned_job_membership():
    rows = {10: row(1, 100), 11: row(10, 101), 90: row(11, 102, owned=False), 91: row(90, 103)}
    assert verified_descendant_pids(rows, 10) == {10, 11}


def test_exited_or_unverified_root_cannot_attribute_any_descendants():
    assert verified_descendant_pids({10: row(1, 100, live=False), 11: row(10, 101)}, 10) == set()
    assert verified_descendant_pids({10: row(1, 100, owned=False), 11: row(10, 101)}, 10) == set()
    assert verified_descendant_pids({11: row(10, 101)}, 10) == set()


def test_exited_parent_does_not_capture_children_born_after_its_pid_was_reused():
    rows = {10: row(1, 100), 11: row(10, 101, live=False), 12: row(11, 200)}
    assert verified_descendant_pids(rows, 10) == {10, 11}
