from pass_memo import once_per_list


def test_same_list_is_computed_once_and_a_changed_list_recomputes():
    calls = []

    @once_per_list
    def count(events, project):
        calls.append(project)
        return len(events)

    events = [1, 2]
    assert count(events, 'a') == 2 and count(events, 'a') == 2 and len(calls) == 1
    assert count(events, 'b') == 2 and count(events, 'a') == 2 and len(calls) == 2
    events.append(3)
    assert count(events, 'a') == 3 and len(calls) == 3
    assert count(list(events), 'a') == 3 and len(calls) == 4
