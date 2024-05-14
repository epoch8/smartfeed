MERGER_APPEND_CONFIG = {
    "merger_id": "merger_append_example",
    "type": "merger_append",
    "items": [
        {
            "subfeed_id": "subfeed_merger_append_example",
            "type": "subfeed",
            "method_name": "ads",
            "subfeed_params": {
                "limit_to_return": 5,
            },
        },
        {
            "subfeed_id": "subfeed_2_merger_append_example",
            "type": "subfeed",
            "method_name": "followings",
        },
    ],
}

MERGER_PERCENTAGE_CONFIG = {
    "merger_id": "merger_percentage_example",
    "type": "merger_percentage",
    "shuffle": False,
    "items": [
        {
            "percentage": 40,
            "data": {
                "subfeed_id": "subfeed_merger_percentage_example",
                "type": "subfeed",
                "method_name": "followings",
            },
        },
        {
            "percentage": 60,
            "data": {
                "subfeed_id": "subfeed_2_merger_percentage_example",
                "type": "subfeed",
                "method_name": "ads",
            },
        },
    ],
}

MERGER_POSITIONAL_CONFIG = {
    "merger_id": "merger_positional_example",
    "type": "merger_positional",
    "positions": [1, 3, 15],
    "start": 17,
    "end": 200,
    "step": 2,
    "positional": {
        "subfeed_id": "subfeed_positional_merger_positional_example",
        "type": "subfeed",
        "method_name": "ads",
        "subfeed_params": {
            "limit_to_return": 10,
        },
    },
    "default": {
        "subfeed_id": "subfeed_default_merger_positional_example",
        "type": "subfeed",
        "method_name": "followings",
    },
}

MERGER_PERCENTAGE_GRADIENT_CONFIG = {
    "merger_id": "merger_percentage_gradient_example",
    "type": "merger_percentage_gradient",
    "item_from": {
        "percentage": 80,
        "data": {
            "subfeed_id": "subfeed_from_merger_percentage_gradient_example",
            "type": "subfeed",
            "method_name": "ads",
        },
    },
    "item_to": {
        "percentage": 20,
        "data": {
            "subfeed_id": "subfeed_to_merger_percentage_gradient_example",
            "type": "subfeed",
            "method_name": "followings",
        },
    },
    "step": 10,
    "size_to_step": 10,
    "shuffle": False,
}

MERGER_VIEW_SESSION_CONFIG = {
    "merger_id": "merger_view_session_example",
    "type": "merger_view_session",
    "session_size": 800,
    "session_live_time": 300,
    "data": {
        "subfeed_id": "subfeed_merger_view_session_example",
        "type": "subfeed",
        "method_name": "followings",
    },
}

MERGER_VIEW_SESSION_DUPS_CONFIG = {
    "merger_id": "merger_view_session_example",
    "type": "merger_view_session",
    "deduplicate": True,
    "session_size": 10,
    "session_live_time": 300,
    "data": {
        "subfeed_id": "subfeed_merger_view_session_example",
        "type": "subfeed",
        "method_name": "doubles",
    },
}

MERGER_VIEW_SESSION_PARTIAL_CONFIG = {
    "merger_id": "merger_view_session_partial_example",
    "type": "merger_view_session_partial",
    "session_size": 800,
    "session_live_time": 300,
    "data": {
        "subfeed_id": "subfeed_merger_view_session_partial_example",
        "type": "subfeed",
        "method_name": "followings",
    },
}
