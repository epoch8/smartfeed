from smartfeed.schemas import FeedResultClient, FeedResultNextPage


def _effective_limit(limit, max_per_call):
    effective_limit = limit
    if isinstance(max_per_call, int) and max_per_call > 0:
        effective_limit = min(effective_limit, max_per_call)
    return effective_limit


def make_offset_paged_method(items, *, max_per_call=None):
    async def _method(user_id, limit, next_page, **kwargs):  # pylint: disable=unused-argument
        offset = int(next_page.after or 0)
        effective_limit = _effective_limit(limit, max_per_call)
        result_data = items[offset : offset + effective_limit]
        next_page.after = offset + len(result_data)
        next_page.page += 1
        has_next_page = (offset + len(result_data)) < len(items)
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=has_next_page)

    return _method


def make_string_after_paged_method(items, *, max_per_call=None, after_field="created_at"):
    """A subfeed method whose cursor is a string (e.g. timestamp).

    Cursor semantics: `after` is the last returned `created_at` value (monotonic).
    """

    async def _method(user_id, limit, next_page, **kwargs):  # pylint: disable=unused-argument
        effective_limit = _effective_limit(limit, max_per_call)

        after = next_page.after
        start_idx = 0
        if isinstance(after, str) and after:
            # Find first item with created_at > after
            for i, item in enumerate(items):
                if str(item[after_field]) > after:
                    start_idx = i
                    break
            else:
                start_idx = len(items)

        result_data = items[start_idx : start_idx + effective_limit]
        has_next_page = (start_idx + len(result_data)) < len(items)

        if result_data:
            next_page.after = str(result_data[-1][after_field])
        next_page.page += 1
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=has_next_page)

    return _method


def make_profile_dict_after_method(
    profiles_to_items,
    *,
    max_per_call=None,
    after_key="after",
):
    """A subfeed method whose cursor is a dict of per-profile offsets.

    Example shape: after = {"p1": 0, "p2": 0}
    Cursor semantics: each profile offset increments as items are *read*.
    """

    profile_ids = list(profiles_to_items.keys())

    async def _method(user_id, limit, next_page, **kwargs):  # pylint: disable=unused-argument
        effective_limit = _effective_limit(limit, max_per_call)

        after = next_page.after
        if not isinstance(after, dict):
            after = {pid: 0 for pid in profile_ids}
        else:
            after = dict(after)
            for pid in profile_ids:
                after.setdefault(pid, 0)

        result = []
        has_next_page = False

        # Build a cyclic iteration over profiles.
        active_profiles = [pid for pid in profile_ids]

        i = 0
        while active_profiles and len(result) < effective_limit:
            pid = active_profiles[i % len(active_profiles)]
            idx = after.get(pid, 0)
            items = profiles_to_items.get(pid, [])

            if idx >= len(items):
                # This profile is exhausted.
                active_profiles.remove(pid)
                continue

            result.append(items[idx])
            after[pid] = idx + 1
            i += 1

        # Determine if any profile still has unread items.
        for pid in profile_ids:
            if after.get(pid, 0) < len(profiles_to_items.get(pid, [])):
                has_next_page = True
                break

        next_page.after = after
        next_page.page += 1
        return FeedResultClient(data=result, next_page=next_page, has_next_page=has_next_page)

    return _method


def _assert_cursor_monotonic_if_present(res_1, res_2, keys):
    for key in keys:
        if key not in res_1.next_page.data:
            continue

        assert key in res_2.next_page.data

        after_1 = res_1.next_page.data[key].after
        after_2 = res_2.next_page.data[key].after

        if after_1 is None or after_2 is None:
            continue

        if isinstance(after_1, int) and isinstance(after_2, int):
            assert after_2 >= after_1
            continue

        if isinstance(after_1, dict) and isinstance(after_2, dict):
            continue

        try:
            assert after_2 >= after_1
        except TypeError:
            pass


def _sources(data):
    return [x.get("src") for x in data]


def _ids(data):
    return [x.get("id") for x in data]


def _assert_no_dupes_in_page(data):
    ids = _ids(data)
    assert len(ids) == len(set(ids))


def _assert_pages_no_overlap(res_1, res_2):
    assert not (set(_ids(res_1.data)) & set(_ids(res_2.data)))


def _assert_two_pages_no_dupes(res_1, res_2):
    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)


def _assert_sources_at_positions(data, positions, expected_src):
    sources = _sources(data)
    for pos in positions:
        assert sources[pos - 1] == expected_src


def make_items(src, start, end, *, user_id_mod=None, id_offset=0, extra=None):
    items = []
    for i in range(start, end):
        item_id = id_offset + i
        item = {"id": item_id, "src": src}
        if user_id_mod is not None:
            item["user_id"] = f"u{item_id % user_id_mod}"
        if extra:
            item.update(extra)
        items.append(item)
    return items


def _subfeed(subfeed_id, method_name, *, dedup_priority=None):
    data = {"subfeed_id": subfeed_id, "type": "subfeed", "method_name": method_name}
    if dedup_priority is not None:
        data["dedup_priority"] = dedup_priority
    return data


def _dedup_config(merger_id, data, *, dedup_key="id", state_backend="cursor", cursor_compress=True, **kwargs):
    config = {
        "merger_id": merger_id,
        "type": "merger_deduplication",
        "dedup_key": dedup_key,
        "state_backend": state_backend,
        "cursor_compress": cursor_compress,
        "data": data,
    }
    config.update(kwargs)
    return config


def _percentage_config(merger_id, items, *, shuffle=False):
    return {"merger_id": merger_id, "type": "merger_percentage", "shuffle": shuffle, "items": items}


def _append_config(merger_id, items, *, shuffle=False):
    return {"merger_id": merger_id, "type": "merger_append", "shuffle": shuffle, "items": items}


def _distribute_config(merger_id, items, *, distribution_key="user_id"):
    return {
        "merger_id": merger_id,
        "type": "merger_distribute",
        "distribution_key": distribution_key,
        "items": items,
    }


def _positional_config(merger_id, *, positions, positional, default):
    return {
        "merger_id": merger_id,
        "type": "merger_positional",
        "positions": positions,
        "positional": positional,
        "default": default,
    }


def _gradient_config(
    merger_id,
    *,
    item_from,
    item_to,
    step,
    size_to_step,
    shuffle=False,
):
    return {
        "merger_id": merger_id,
        "type": "merger_percentage_gradient",
        "item_from": item_from,
        "item_to": item_to,
        "step": step,
        "size_to_step": size_to_step,
        "shuffle": shuffle,
    }


async def _run_two_pages(merger, methods_dict, limit, *, next_page=None, **kwargs):
    if next_page is None:
        next_page = FeedResultNextPage(data={})
    res_1 = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=limit, next_page=next_page, **kwargs)
    res_2 = await merger.get_data(
        methods_dict=methods_dict, user_id="u", limit=limit, next_page=res_1.next_page, **kwargs
    )
    return res_1, res_2


def _percentage_items(first, second, *, first_pct=50, second_pct=50):
    return [
        {"percentage": first_pct, "data": first},
        {"percentage": second_pct, "data": second},
    ]


def _two_subfeed_spec(*, name="a", subfeed_id="sf_a", max_per_call=None, dedup_priority=None):
    return {
        "name": name,
        "subfeed_id": subfeed_id,
        "max_per_call": max_per_call,
        "dedup_priority": dedup_priority,
    }


def _build_two_subfeed_methods(items_a, items_b, *, spec_a=None, spec_b=None):
    if spec_a is None:
        spec_a = _two_subfeed_spec()
    if spec_b is None:
        spec_b = _two_subfeed_spec(name="b", subfeed_id="sf_b")

    methods_dict = {
        spec_a["name"]: make_offset_paged_method(items_a, max_per_call=spec_a["max_per_call"]),
        spec_b["name"]: make_offset_paged_method(items_b, max_per_call=spec_b["max_per_call"]),
    }
    subfeed_a = _subfeed(spec_a["subfeed_id"], spec_a["name"], dedup_priority=spec_a["dedup_priority"])
    subfeed_b = _subfeed(spec_b["subfeed_id"], spec_b["name"], dedup_priority=spec_b["dedup_priority"])
    return methods_dict, subfeed_a, subfeed_b


def _build_two_subfeed_dedup_merger(
    *,
    items_a,
    items_b,
    child_builder,
    merger_id,
    spec_a=None,
    spec_b=None,
    dedup_kwargs=None,
):
    methods_dict, subfeed_a, subfeed_b = _build_two_subfeed_methods(
        items_a,
        items_b,
        spec_a=spec_a,
        spec_b=spec_b,
    )
    config = _dedup_config(merger_id, child_builder(subfeed_a, subfeed_b), **(dedup_kwargs or {}))
    return config, methods_dict, subfeed_a, subfeed_b


def _build_deep_positional_pct_dedup_merger(
    *,
    items_p,
    items_d1,
    items_d2,
    dedup_merger_id,
    pos_merger_id,
    pct_merger_id,
    positions,
    overfetch_factor=None,
    max_refill_loops=None,
):
    methods_dict = {
        "p": make_offset_paged_method(items_p),
        "d1": make_offset_paged_method(items_d1),
        "d2": make_offset_paged_method(items_d2),
    }

    dedup_kwargs = {}
    if overfetch_factor is not None:
        dedup_kwargs["overfetch_factor"] = overfetch_factor
    if max_refill_loops is not None:
        dedup_kwargs["max_refill_loops"] = max_refill_loops

    config = _dedup_config(
        dedup_merger_id,
        _positional_config(
            pos_merger_id,
            positions=positions,
            positional=_subfeed("sf_p", "p"),
            default=_percentage_config(
                pct_merger_id,
                items=_percentage_items(_subfeed("sf_d1", "d1"), _subfeed("sf_d2", "d2")),
            ),
        ),
        **dedup_kwargs,
    )
    return config, methods_dict


def _inner_append_config(*, merger_id: str, subfeed_id: str, method_name: str, dedup_priority: int):
    return {
        "merger_id": merger_id,
        "type": "merger_append",
        # Important: dedup deletion priority must be visible at this node so parent mergers
        # can fetch higher-priority subtrees first when a dedup wrapper is active.
        "dedup_priority": dedup_priority,
        "shuffle": False,
        "items": [
            {
                "subfeed_id": subfeed_id,
                "type": "subfeed",
                "method_name": method_name,
                "dedup_priority": dedup_priority,
            }
        ],
    }


def _build_deep_priority_tree_for_merger_type(*, merger_type: str):
    """Return a deep tree config where low/high leaves overlap by id.

    The inner leaves are wrapped into an append merger to ensure a "deep" tree even
    when the outer merger is flat.
    """

    low = _inner_append_config(merger_id="inner_low", subfeed_id="sf_low", method_name="low", dedup_priority=0)
    high = _inner_append_config(merger_id="inner_high", subfeed_id="sf_high", method_name="high", dedup_priority=100)

    if merger_type == "merger_append":
        return {
            "merger_id": "outer_append",
            "type": "merger_append",
            "shuffle": False,
            # Put low first intentionally; priority must still make high win for overlapping ids.
            "items": [low, high],
        }

    if merger_type == "merger_distribute":
        return {
            "merger_id": "outer_dist",
            "type": "merger_distribute",
            "distribution_key": "user_id",
            # Put low first intentionally.
            "items": [low, high],
        }

    if merger_type == "merger_percentage":
        return {
            "merger_id": "outer_pct",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {"percentage": 50, "data": low},
                {"percentage": 50, "data": high},
            ],
        }

    if merger_type == "merger_percentage_gradient":
        return {
            "merger_id": "outer_grad",
            "type": "merger_percentage_gradient",
            "item_from": {"percentage": 60, "data": low},
            "item_to": {"percentage": 40, "data": high},
            "step": 20,
            "size_to_step": 5,
            "shuffle": False,
        }

    if merger_type == "merger_positional":
        # High priority on positional branch so it must win duplicates.
        high_pos = _inner_append_config(
            merger_id="inner_pos_high",
            subfeed_id="sf_high",
            method_name="high",
            dedup_priority=100,
        )
        low_def = _inner_append_config(
            merger_id="inner_def_low",
            subfeed_id="sf_low",
            method_name="low",
            dedup_priority=0,
        )
        return {
            "merger_id": "outer_pos",
            "type": "merger_positional",
            "positions": [1, 3, 5, 7, 9, 11],
            "positional": high_pos,
            "default": low_def,
        }

    raise AssertionError(f"Unknown merger_type: {merger_type}")
